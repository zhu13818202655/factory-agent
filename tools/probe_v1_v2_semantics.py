"""实测 V1/V2 的**拒绝语义**与**缓存键归一化三项**（离线一次性脚本，直连客户 MES）。

用法（仓库根目录）：

    set -a; source .env; set +a
    .venv/bin/python tools/probe_v1_v2_semantics.py --all
    .venv/bin/python tools/probe_v1_v2_semantics.py --case t9
    .venv/bin/python tools/probe_v1_v2_semantics.py --case n1 --case n2 --case n3

四组实测：

- **T9 拒绝语义**：`00` 员工对 V1 `GongziJeOrderQuery` / `YskQuery` / `GongziMxQuery` 与
  V2 `GongziClQuery` / `ScjdQuery` 的实际返回码与行数。
- **N1 排序不进键**：服务端 `orderbyFields` 的结果能否由本地对「默认顺序结果」重排复现；
  同参重复调用时顺序是否稳定（决定能否把排序从缓存键里摘掉）。
- **N2 `groupby` 集合化**：同一字段集合的不同传入顺序，行集与列序是否一致
  （决定键能否按「排序后的字段名集合」生成）。
- **N3 过滤取值归一化**：`Type` 的取值顺序（`0,1,2` vs `2,1,0`）是否影响结果
  （决定键能否对枚举取值排序去重）。

脱敏：控制台**只输出返回码、行数、列名、布尔与相对偏差**；姓名与金额列一律打码。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from direct_mes_client import (  # noqa: E402
    BASE,
    get_token,
    gongzi_je_order_query,
    gongzi_mx_query,
    ysk_query,
)

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / ".cache" / "semantics_raw"

GONGZI_V2 = "/api/NetYf/Sclzd/v2/GongziClQuery"
SCJD_V2 = "/api/NetYf/Sclzd/v2/ScjdQuery"

ROLE_LABEL = {"00": "员工", "01": "组长", "02": "管理", "99": "老板"}

#: 控制台打码列（Confidential）。
MASKED = frozenset({"uname", "je", "price", "mobile", "loginUserName", "name_pk"})

#: 小窗口，够产生多行、又不至于拉大量数据。
DATES = "2026-07-15"
DATEE = "2026-07-15"

TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def rows_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("result") or payload.get("data") or {}
    if isinstance(body, dict):
        for key in ("list", "employeeList", "deptList"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def envelope(payload: dict[str, Any]) -> tuple[Any, Any, int]:
    return payload.get("code"), payload.get("message"), len(rows_of(payload))


def call_v2(role: str, path: str, business: dict[str, Any]) -> tuple[dict[str, Any], float]:
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
    return resp.json(), time.perf_counter() - started


def keys_of(rows: list[dict[str, Any]], exclude: frozenset[str]) -> set[tuple]:
    """把行投影为「非度量列」的元组集合，用于跨请求比对行集。

    列名先**排序**再取元组 —— 否则响应列序不同会让相同数据被判为不同。
    """
    if not rows:
        return set()
    metric = {"bs", "fhsl", "sssl", "sl", "zpsl", "wgs", "wts", "wcl", "je", "bgs", "bts"}
    cols = sorted(c for c in rows[0] if c not in metric and c not in exclude)
    return {tuple(str(r.get(c)) for c in cols) for r in rows}


def dump(name: str, payload: dict[str, Any]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------------------------------------------------------------------------
# T9 · 拒绝语义
# ---------------------------------------------------------------------------
def case_t9() -> None:
    head("T9 · 拒绝语义：V1 vs V2 对各角色的实际返回")

    print("\n  V1 `GongziJeOrderQuery`（现行 FR-004/008 的收入排名来源）")
    print(f"  {'角色':10s} {'code':>8s} {'message':>14s} {'行数':>8s}")
    for role in ("00", "01", "02", "99"):
        try:
            payload = gongzi_je_order_query(role, dates=DATES, datee=DATEE)
            code, message, n = envelope(payload)
            print(f"  {role} {ROLE_LABEL[role]:6s} {str(code):>8s} {str(message)[:14]:>14s} {n:8d}")
        except SystemExit as exc:
            print(f"  {role} {ROLE_LABEL[role]:6s} 调用失败：{exc}")

    print("\n  V1 `YskQuery`（现行 FR-006/007/010/013 的产量来源）")
    print(f"  {'角色':10s} {'code':>8s} {'message':>14s} {'行数':>8s}")
    for role in ("00", "01", "02", "99"):
        try:
            payload = ysk_query(role, dates=DATES, datee=DATEE)
            code, message, n = envelope(payload)
            print(f"  {role} {ROLE_LABEL[role]:6s} {str(code):>8s} {str(message)[:14]:>14s} {n:8d}")
        except SystemExit as exc:
            print(f"  {role} {ROLE_LABEL[role]:6s} 调用失败：{exc}")

    print("\n  V1 `GongziMxQuery`（明细，不传 Uid）")
    print(f"  {'角色':10s} {'code':>8s} {'message':>14s} {'行数':>8s}")
    for role in ("00", "01", "02", "99"):
        try:
            payload = gongzi_mx_query(role, uid="", dates=DATES, datee=DATEE)
            code, message, n = envelope(payload)
            print(f"  {role} {ROLE_LABEL[role]:6s} {str(code):>8s} {str(message)[:14]:>14s} {n:8d}")
        except SystemExit as exc:
            print(f"  {role} {ROLE_LABEL[role]:6s} 调用失败：{exc}")

    print("\n  V2 `GongziClQuery`（无过滤参数）")
    print(f"  {'角色':10s} {'code':>8s} {'message':>14s} {'行数':>8s}")
    for role in ("00", "01", "02", "99"):
        try:
            payload, _ = call_v2(role, GONGZI_V2, {"dates": DATES, "datee": DATEE, "Flag": "0"})
            code, message, n = envelope(payload)
            print(f"  {role} {ROLE_LABEL[role]:6s} {str(code):>8s} {str(message)[:14]:>14s} {n:8d}")
        except SystemExit as exc:
            print(f"  {role} {ROLE_LABEL[role]:6s} 调用失败：{exc}")

    print("\n  V2 `ScjdQuery`（无过滤参数；文档称 00 禁止查询）")
    print(f"  {'角色':10s} {'code':>8s} {'message':>14s} {'行数':>8s}")
    for role in ("00", "01", "02", "99"):
        try:
            payload, _ = call_v2(role, SCJD_V2, {"dates": DATES, "datee": DATEE})
            code, message, n = envelope(payload)
            print(f"  {role} {ROLE_LABEL[role]:6s} {str(code):>8s} {str(message)[:14]:>14s} {n:8d}")
        except SystemExit as exc:
            print(f"  {role} {ROLE_LABEL[role]:6s} 调用失败：{exc}")


# ---------------------------------------------------------------------------
# N1 · 排序不进键
# ---------------------------------------------------------------------------
def case_n1(dates: str = "2026-07-01", datee: str = "2026-07-31") -> None:
    head("N1 · 排序不进键：服务端排序能否本地复现 + 顺序是否稳定")
    role = "99"
    base = {"dates": dates, "datee": datee}
    gb = ["dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"]
    print(f"  窗口 {dates} ~ {datee}（需要 wcl 有区分度，故用月窗而非日窗）")

    payload_a, _ = call_v2(role, SCJD_V2, {**base, "groupbyFields": gb})
    payload_a2, _ = call_v2(role, SCJD_V2, {**base, "groupbyFields": gb})
    payload_b, _ = call_v2(
        role, SCJD_V2, {**base, "groupbyFields": gb, "orderbyFields": ["wcl:desc"]}
    )
    payload_c, _ = call_v2(
        role, SCJD_V2, {**base, "groupbyFields": gb, "orderbyFields": ["wcl:asc"]}
    )

    rows_a = rows_of(payload_a)
    rows_a2 = rows_of(payload_a2)
    rows_b = rows_of(payload_b)
    rows_c = rows_of(payload_c)

    if not rows_a:
        print("  空结果，跳过。")
        return

    key_cols = [
        c for c in rows_a[0] if c not in {"wcl", "bs", "fhsl", "sssl", "zpsl", "wgs", "wts"}
    ]
    seq_a = [tuple(str(r.get(c)) for c in key_cols) for r in rows_a]
    seq_a2 = [tuple(str(r.get(c)) for c in key_cols) for r in rows_a2]
    seq_b = [tuple(str(r.get(c)) for c in key_cols) for r in rows_b]
    seq_c = [tuple(str(r.get(c)) for c in key_cols) for r in rows_c]

    print(
        f"  行数：默认 {len(rows_a)} / 默认(重跑) {len(rows_a2)}"
        f" / wcl:desc {len(rows_b)} / wcl:asc {len(rows_c)}"
    )
    print(f"  ① 默认顺序两次调用完全一致      : {seq_a == seq_a2}")
    print(f"  ② 默认 与 wcl:desc 行集相同      : {set(seq_a) == set(seq_b)}")
    print(f"  ③ 默认 与 wcl:asc  行集相同      : {set(seq_a) == set(seq_c)}")
    print(f"  ④ wcl:desc 与 wcl:asc 互为逆序   : {seq_b == list(reversed(seq_c))}")

    local_desc = [
        tuple(str(r.get(c)) for c in key_cols)
        for r in sorted(rows_a, key=lambda r: -float(r.get("wcl") or 0))
    ]
    local_asc = [
        tuple(str(r.get(c)) for c in key_cols)
        for r in sorted(rows_a, key=lambda r: float(r.get("wcl") or 0))
    ]
    print(f"  ⑤ 服务端 wcl:desc == 本地稳定降序 : {seq_b == local_desc}")
    print(f"  ⑥ 服务端 wcl:asc  == 本地稳定升序 : {seq_c == local_asc}")

    wcl_values = [float(r.get("wcl") or 0) for r in rows_a]
    ties = len(wcl_values) - len(set(wcl_values))
    print(f"  ⑦ 排序键 wcl 的取值数 {len(set(wcl_values))} / 行数 {len(wcl_values)}，并列 {ties}")
    if ties:
        print("     ⚠️ 存在并列 → 服务端与本地必须用同一套并列打破规则，否则顺序不可复现")

    dump("n1-default", payload_a)
    dump("n1-wcl-desc", payload_b)


# ---------------------------------------------------------------------------
# N2 · groupby 集合化
# ---------------------------------------------------------------------------
def case_n2(dates: str = "2026-07-01", datee: str = "2026-07-31") -> None:
    head("N2 · groupby 集合化：同一字段集合的不同传入顺序")
    role = "99"
    base = {"dates": dates, "datee": datee}
    pairs = [
        (["dh", "dept", "deptname"], ["dept", "deptname", "dh"]),
        (["dh", "jhdh", "dept"], ["dept", "dh", "jhdh"]),
    ]
    for left, right in pairs:
        pa, _ = call_v2(role, SCJD_V2, {**base, "groupbyFields": left})
        pb, _ = call_v2(role, SCJD_V2, {**base, "groupbyFields": right})
        ra, rb = rows_of(pa), rows_of(pb)
        if not ra or not rb:
            print(f"\n  {left} vs {right}：空结果，跳过。")
            continue
        ca, cb = list(ra[0].keys()), list(rb[0].keys())
        same_set = set(ca) == set(cb)
        same_order = ca == cb
        ka = keys_of(ra, MASKED)
        kb = keys_of(rb, MASKED)
        print(f"\n  {left}")
        print(f"  {right}")
        print(f"    行数 {len(ra)} / {len(rb)}   列集合相同 {same_set}   列序相同 {same_order}")
        print(f"    行集（按非度量列投影）相同：{ka == kb}")
        if not same_order:
            print(f"    列序差异：{ca}  vs  {cb}")
        dump(f"n2-{'-'.join(left)}", pa)
        dump(f"n2-{'-'.join(right)}", pb)


# ---------------------------------------------------------------------------
# N3 · 过滤取值归一化
# ---------------------------------------------------------------------------
def case_n3() -> None:
    head("N3 · 过滤取值归一化：Type 枚举的传入顺序")
    role = "99"
    base = {"dates": DATES, "datee": DATEE, "Flag": "0"}
    variants = ["0,1,2", "2,1,0", "0,2,1"]
    results: dict[str, tuple[int, set[tuple], dict[str, Any]]] = {}
    for v in variants:
        payload, _ = call_v2(role, GONGZI_V2, {**base, "Type": v})
        rows = rows_of(payload)
        footer = (payload.get("result") or {}).get("footer") or {}
        results[v] = (len(rows), keys_of(rows, MASKED), footer)
        dump(f"n3-type-{v.replace(',', '_')}", payload)

    print(f"\n  {'Type':10s} {'code':>6s} {'行数':>8s}")
    for v in variants:
        print(f"  {v:10s} {'-':>6s} {results[v][0]:8d}")

    base_rows, base_keys, base_footer = results[variants[0]]
    for v in variants[1:]:
        n, keys, footer = results[v]
        print(f"\n  {variants[0]} vs {v}：行数相同 {n == base_rows}   行集相同 {keys == base_keys}")
        diff = {
            k: (base_footer.get(k), footer.get(k))
            for k in set(base_footer) | set(footer)
            if base_footer.get(k) != footer.get(k)
        }
        print(f"    footer 差异键数 {len(diff)}" + (f"：{diff}" if diff else "（完全一致）"))


CASES = {"t9": case_t9, "n1": case_n1, "n2": case_n2, "n3": case_n3}


def main() -> None:
    parser = argparse.ArgumentParser(description="实测 V1/V2 拒绝语义与缓存键归一化三项")
    parser.add_argument("--case", action="append", choices=sorted(CASES), default=None)
    parser.add_argument("--all", action="store_true", help="跑全部四组")
    args = parser.parse_args()

    selected = sorted(CASES) if (args.all or not args.case) else args.case
    print(f"BASE={BASE}\n窗口 {DATES} ~ {DATEE}\n用例 {selected}")
    for name in selected:
        try:
            CASES[name]()
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [{name}] 失败：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
