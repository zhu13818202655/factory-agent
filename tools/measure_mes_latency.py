"""实测客户 MES V2 接口的**单次调用耗时**与**范围列空值率**（离线一次性脚本，直连 MES）。

用法（仓库根目录）：

    set -a; source .env; set +a
    .venv/bin/python tools/measure_mes_latency.py
    .venv/bin/python tools/measure_mes_latency.py --role 99 --family progress
    .venv/bin/python tools/measure_mes_latency.py --raw      # 额外落盘原始响应

回答两个问题：

1. **瓶颈在 MES 段还是 LLM 段** —— 本脚本只测 MES 段单次耗时（不含 LLM）。
   生产埋点 ``usage_event`` 已同时记录 ``llm_duration_ms`` / ``mes_duration_ms``，
   两者应交叉印证。
2. **``dept`` / ``uid`` 空值是否普遍** —— 同一族在多个时间窗下的空值率与样例归属。

脱敏：控制台**只输出计数、占比、耗时与布尔**，不输出金额、姓名、员工号或部门号取值。
"""

from __future__ import annotations

import argparse
import json
import statistics as stats
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from direct_mes_client import BASE, get_token  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / ".cache" / "latency_raw"

GONGZI_V2 = "/api/NetYf/Sclzd/v2/GongziClQuery"
SCJD_V2 = "/api/NetYf/Sclzd/v2/ScjdQuery"

ROLE_LABEL = {"00": "员工", "01": "组长", "02": "管理", "99": "老板"}

#: 实测案例：族 × 窗口 × 粒度。窗口刻意覆盖「单日 / 单周 / 单月 / 一年」四档。
CASES: list[dict[str, Any]] = [
    {"label": "进度·单日", "family": "progress", "dates": "2026-07-15", "datee": "2026-07-15"},
    {"label": "进度·单周", "family": "progress", "dates": "2026-07-01", "datee": "2026-07-07"},
    {"label": "进度·单月", "family": "progress", "dates": "2026-07-01", "datee": "2026-07-31"},
    {
        "label": "进度·单月·制单×部门",
        "family": "progress",
        "dates": "2026-07-01",
        "datee": "2026-07-31",
        "groupby": ["dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"],
    },
    {"label": "工资·单日", "family": "wage", "dates": "2026-07-15", "datee": "2026-07-15"},
    {"label": "工资·单月", "family": "wage", "dates": "2026-07-01", "datee": "2026-07-31"},
    {"label": "工资·一年", "family": "wage", "dates": "2025-10-11", "datee": "2026-07-31"},
]

#: 范围列。空值 = JSON null / 空串 / 未出现在行里。
SCOPE_COLS = ("uid", "uname", "dept", "deptname")
#: 时间锚列（进度侧没有日期列，用 dh 前 8 位）。
ANCHOR_COLS = ("dh", "jhdh", "inputdate", "check_time")

TIMEOUT = 300.0


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def rows_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("result") or payload.get("data") or {}
    rows = body.get("list") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(
            f"未识别载荷：code={payload.get('code')} message={payload.get('message')!r}"
        )
    return rows


def call(role: str, path: str, business: dict[str, Any]) -> tuple[dict[str, Any], float, int]:
    """返回（响应, 秒, 字节数）。"""
    token = get_token(role)
    body = {
        "app_key": token["appkey"],
        "timestamp": token["timestamp"],
        "sign": token["sign"],
        **business,
    }
    started = time.perf_counter()
    resp = httpx.post(
        f"{BASE}{path}",
        json=body,
        headers={"Authorization": f"Bearer {token['accessToken']}"},
        timeout=TIMEOUT,
    )
    elapsed = time.perf_counter() - started
    return resp.json(), elapsed, len(resp.content)


def business_of(case: dict[str, Any]) -> dict[str, Any]:
    biz: dict[str, Any] = {"dates": case["dates"], "datee": case["datee"]}
    if case["family"] == "wage":
        biz |= {"Flag": "0", "Type": "0,1,2"}
    if case.get("groupby"):
        # 契约：groupbyFields 是 List<string>，必须发数组（发逗号串会被判为参数非法）。
        biz["groupbyFields"] = list(case["groupby"])
    return biz


def report(
    case: dict[str, Any], role: str, payload: dict[str, Any], secs: float, nbytes: int
) -> None:
    rows = rows_of(payload)
    n = len(rows) or 1
    print(
        f"\n  [{role} {ROLE_LABEL.get(role, '?')}] {case['label']}  "
        f"{case['dates']} ~ {case['datee']}"
    )
    print(f"    耗时 {secs:7.2f} s   体积 {nbytes / 1024 / 1024:8.2f} MB   行数 {len(rows):7d}")

    if not rows:
        print("    （空结果，跳过空值统计）")
        return

    for col in SCOPE_COLS:
        present = [r for r in rows if col in r]
        empty = sum(1 for r in rows if is_empty(r.get(col)))
        uniq = len({str(r.get(col)) for r in rows if not is_empty(r.get(col))})
        flag = "  ← 有列" if present else "  ← 行里无此列"
        print(f"    {col:9s} 空 {empty:7d} ({empty / n * 100:5.2f}%)  非空唯一值 {uniq:5d}{flag}")

    for col in ANCHOR_COLS:
        if col not in rows[0]:
            continue
        empty = sum(1 for r in rows if is_empty(r.get(col)))
        print(f"    {col:9s} 空 {empty:7d} ({empty / n * 100:5.2f}%)  （时间锚列）")

    # 进度侧：dh 前 8 位分布（验证时间锚与窗口一致）
    if "dh" in rows[0]:
        prefixed = [str(r.get("dh"))[:8] for r in rows if r.get("dh")]
        if prefixed:
            lo, hi = min(prefixed), max(prefixed)
            span = len(set(prefixed))
            out_of_range = sum(
                1
                for p in prefixed
                if not (case["dates"].replace("-", "") <= p <= case["datee"].replace("-", ""))
            )
            print(f"    dh 前8位  范围 {lo} ~ {hi}  跨 {span} 天  越界 {out_of_range} 行")

    # 孤儿行的工序归属（仅统计，不输出取值）
    if "dept" in rows[0]:
        orphans = [r for r in rows if is_empty(r.get("dept"))]
        if orphans:
            wt = Counter(str(r.get("wtname")) for r in orphans)
            dh_n = len({str(r.get("dh")) for r in orphans})
            allorphan = sum(
                1
                for d, c in Counter(str(r.get("dh")) for r in rows).items()
                if len({is_empty(x.get("dept")) for x in rows if str(x.get("dh")) == d}) == 1
                and is_empty(next(x for x in rows if str(x.get("dh")) == d).get("dept"))
            )
            print(f"    孤儿行 {len(orphans)} 行 / {dh_n} 个制单 / 全孤儿制单 {allorphan}")
            print(f"    孤儿行工序名分布 top5: {[f'{k}:{v}' for k, v in wt.most_common(5)]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="实测 MES V2 单次调用耗时与范围列空值率")
    parser.add_argument("--role", default="99", choices=sorted(ROLE_LABEL))
    parser.add_argument("--family", default="all", choices=["all", "wage", "progress"])
    parser.add_argument("--raw", action="store_true", help="落盘原始响应到 .cache/latency_raw/")
    args = parser.parse_args()

    cases = [c for c in CASES if args.family == "all" or c["family"] == args.family]

    print(f"BASE={BASE}")
    print(
        f"角色 {args.role} ({ROLE_LABEL[args.role]})   超时 {TIMEOUT:.0f}s   案例 {len(cases)} 个"
    )

    timings: list[tuple[str, float, int, int]] = []
    for case in cases:
        path = GONGZI_V2 if case["family"] == "wage" else SCJD_V2
        biz = business_of(case)
        try:
            payload, secs, nbytes = call(args.role, path, biz)
            report(case, args.role, payload, secs, nbytes)
            timings.append((case["label"], secs, len(rows_of(payload)), nbytes))
            if args.raw:
                RAW_DIR.mkdir(parents=True, exist_ok=True)
                slug = f"{args.role}-{case['family']}-{case['dates']}-{case['datee']}"
                (RAW_DIR / f"{slug}.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [{args.role}] {case['label']} 失败：{type(exc).__name__}: {exc}")

    print("\n=== 汇总：单次 MES 调用耗时 ===")
    print(f"  {'案例':28s} {'耗时(s)':>9s} {'行数':>9s} {'体积(MB)':>10s}")
    for label, secs, nrows, nbytes in timings:
        print(f"  {label:28s} {secs:9.2f} {nrows:9d} {nbytes / 1024 / 1024:10.2f}")
    if timings:
        values = [t[1] for t in timings]
        print(
            f"\n  中位 {stats.median(values):.2f} s   均值 {stats.mean(values):.2f} s   "
            f"最大 {max(values):.2f} s"
        )


if __name__ == "__main__":
    main()
