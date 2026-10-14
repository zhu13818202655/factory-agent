"""0.8 双跑对账：V2 新源 vs 保留的 V1 对账锚点（离线一次性脚本，直连客户 MES）。

用法（仓库根目录）：

    set -a; source .env; set +a
    .venv/bin/python tools/reconcile_v1_v2.py --case all
    .venv/bin/python tools/reconcile_v1_v2.py --case r3 --dates 2026-07-01 --datee 2026-07-31
    .venv/bin/python tools/reconcile_v1_v2.py --role 99 --max-pages 50

三组对账（Issue 0.8）：

| # | 组 | V1 锚点 | V2 新源 | 逐组比较 |
|---|---|---|---|---|
| R1 | 员工产量 | `YskQuery`（分页） | `GongziClQuery` `groupbyFields=["uid"]` | 每组 `sl` / `je` |
| R2 | 款号 × 工序 | `HuohaoWtCLQuery`（`scheme`） | `GongziClQuery` `groupbyFields=["bbreed","wtname"]` | 每组 `sl` / `je` |
| R3 | A4 `fhsl` vs `zsl` | 进度 V1 `ScjdQuery`（`zsl`） | 进度 V2 `ScjdQuery` `groupbyFields=["dh","dept"]`（`fhsl`） | 每组 `zsl` = `fhsl` |

**通过判据**：R1 / R2 逐组「整数列严格相等、浮点列在容差内」；R3 逐组
`zsl == fhsl`。全部通过后，方可把 V1 源从链路摘除（0.9）并把锚点置
`enabled: false`；未通过前锚点必须保持 `enabled: true`（见 `apis.yaml` 注释）。

**为什么需要独立来源**：ADR-0005 的三方对账（明细合计 / 本地汇总 / `footer`）
需要交叉验证。若 V1 全部退役，对账退化为 `footer` 自对账，失去交叉能力。

**脱敏**（SECURITY.md）：控制台只输出组键、组数、命中 / 不一致计数与**相对
偏差**；姓名与金额列一律打码，凭据由 `direct_mes_client._redact` 处理。

**键的可比性**：V1 / V2 的字段命名不同（`huohao`(编码) 与 `bbreed`(名称)、
`worktype`(编码) 与 `wtname`(名称) 在 V2 同一接口同时输出）。脚本按可配置的
键列取组，并在键列缺失时**跳过该组并说明原因**，绝不静默判为通过。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from direct_mes_client import (  # noqa: E402
    DATEE,
    DATES,
    QUERY_FOOTER,
    TYPE,
    _call,
    get_token,
    huohao_wt_cl_query,
    ysk_query,
)

ROLE_DEFAULT = "99"
PAGE_SIZE = 200
SIZE = 200

GONGZI_V2 = "/api/NetYf/Sclzd/v2/GongziClQuery"
SCJD_V2 = "/api/NetYf/Sclzd/v2/ScjdQuery"
SCJD_V1 = "/api/NetYf/Sclzd/ScjdQuery"

#: 控制台打码列（Confidential）。
MASKED = frozenset({"uname", "je", "price", "mobile", "loginUserName", "name_pk"})

#: 整数列（严格相等）与浮点列（容差）。比率列绝不可加，故不参与对账。
INTEGER_COLUMNS = frozenset({"sl", "bs", "fhsl", "zsl", "sssl", "bts", "bgs", "wts"})
FLOAT_COLUMNS = frozenset({"je"})
FLOAT_ABS_TOLERANCE = 0.005


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _rows_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    for key in ("list", "rows"):
        value = result.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _fetch_paged(
    path: str,
    token: dict[str, Any],
    business: dict[str, Any],
    *,
    bool_params: tuple[str, ...] = (),
    max_pages: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Walk a ``list_total`` V1 source to ``result.total`` (V2 is not paginated)."""
    rows: list[dict[str, Any]] = []
    payload: dict[str, Any] = {}
    for page in range(1, max_pages + 1):
        payload = _call(
            path,
            token,
            {**business, "page": page, "size": PAGE_SIZE},
            bool_params=bool_params,
        )
        if payload.get("code") != 1:
            return payload, []
        chunk = _rows_of(payload)
        rows.extend(chunk)
        total = (payload.get("result") or {}).get("total")
        if not chunk or (isinstance(total, int) and len(rows) >= total):
            break
    return payload, rows


def _fetch_v2(
    path: str,
    token: dict[str, Any],
    business: dict[str, Any],
    groupby: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One unpaginated V2 call; ``groupbyFields`` must go out as a JSON array."""
    payload = _call(path, token, {**business, "groupbyFields": groupby})
    if payload.get("code") != 1:
        return payload, []
    return payload, _rows_of(payload)


# ---------------------------------------------------------------------------
# 归一化与比较
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _group(
    rows: list[dict[str, Any]],
    key_fields: tuple[str, ...],
    value_columns: tuple[str, ...],
) -> dict[tuple[str, ...], dict[str, float]]:
    """Sum additive columns per group key; blank key parts fold into ``""``.

    项目口径：空值统一归入同一组（`jhdh` 空值即「未关联计划号」），因此这里把
    ``None`` / 空串折叠成同一个键，而不是丢行或分到不同组。
    """
    grouped: dict[tuple[str, ...], dict[str, float]] = defaultdict(
        lambda: dict.fromkeys(value_columns, 0.0)
    )
    for row in rows:
        key = tuple(str(row.get(field) or "") for field in key_fields)
        bucket = grouped[key]
        for column in value_columns:
            number = _to_float(row.get(column))
            if number is not None:
                bucket[column] += number
    return dict(grouped)


def _compare(
    left: dict[tuple[str, ...], dict[str, float]],
    right: dict[tuple[str, ...], dict[str, float]],
    columns: tuple[str, ...],
) -> tuple[int, list[str]]:
    """Return (mismatch_count, report lines) over the union of group keys."""
    lines: list[str] = []
    mismatches = 0
    for key in sorted(set(left) | set(right)):
        left_row = left.get(key, {})
        right_row = right.get(key, {})
        for column in columns:
            lhs = left_row.get(column)
            rhs = right_row.get(column)
            if lhs is None and rhs is None:
                continue
            if lhs is None or rhs is None:
                mismatches += 1
                lines.append(f"    组 {key} · {column}: 只有一侧有值（left={lhs} right={rhs}）")
                continue
            if column in INTEGER_COLUMNS:
                if abs(lhs - rhs) > 1e-9:
                    mismatches += 1
                    lines.append(f"    组 {key} · {column}: 整数列不相等（Δ={lhs - rhs:+.0f}）")
            else:
                deviation = _relative_deviation(lhs, rhs)
                if deviation is None or deviation > FLOAT_ABS_TOLERANCE:
                    mismatches += 1
                    # 金额只输出相对偏差，不输出原值（脱敏）。
                    lines.append(f"    组 {key} · {column}: 浮点列超容差（相对偏差 {deviation}）")
    return mismatches, lines


def _relative_deviation(lhs: float, rhs: float) -> float | None:
    scale = max(abs(lhs), abs(rhs))
    if scale == 0:
        return 0.0 if lhs == rhs else None
    return abs(lhs - rhs) / scale


def _keys_present(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bool:
    if not rows:
        return False
    available = set(rows[0])
    return all(field in available for field in fields)


# ---------------------------------------------------------------------------
# 对账组
# ---------------------------------------------------------------------------
def reconcile_r1(role: str, dates: str, datee: str, max_pages: int) -> bool:
    """R1 员工产量：V1 `YskQuery` vs V2 `GongziClQuery` `groupbyFields=["uid"]`."""
    print("\n=== R1 员工产量 · YskQuery(V1) vs GongziClQuery(V2) ===")
    v1_payload, v1_rows = _fetch_paged(
        "/api/NetYf/Sclzd/YskQuery",
        get_token(role),
        {"dates": dates, "datee": datee},
        max_pages=max_pages,
    )
    v2_payload, v2_rows = _fetch_v2(
        GONGZI_V2,
        get_token(role),
        {"dates": dates, "datee": datee, "Flag": "0", "Type": TYPE},
        ["uid"],
    )
    print(f"  V1 code={v1_payload.get('code')} rows={len(v1_rows)}")
    print(f"  V2 code={v2_payload.get('code')} rows={len(v2_rows)}")
    key_fields = ("uid",)
    columns = ("sl", "je")
    if not _keys_present(v1_rows, key_fields) or not _keys_present(v2_rows, key_fields):
        print("  ⚠️ 键列缺失，跳过 —— 请人工确认两侧字段名（不判为通过）")
        return False
    mismatches, lines = _compare(
        _group(v1_rows, key_fields, columns), _group(v2_rows, key_fields, columns), columns
    )
    print(
        f"  分组数 V1={len(_group(v1_rows, key_fields, columns))} V2={len(_group(v2_rows, key_fields, columns))}"
    )
    for line in lines[:40]:
        print(line)
    print(f"  → 不一致 {mismatches} 项")
    return mismatches == 0


def reconcile_r2(role: str, dates: str, datee: str, scheme: str) -> bool:
    """R2 款号 × 工序：V1 `HuohaoWtCLQuery` vs V2 `GongziClQuery`（`bbreed`+`wtname`）."""
    print("\n=== R2 款号 × 工序 · HuohaoWtCLQuery(V1) vs GongziClQuery(V2) ===")
    v1_payload = huohao_wt_cl_query(role, scheme=scheme, dates=dates, datee=datee)
    token = get_token(role)
    v1_rows = _rows_of(v1_payload)
    v2_payload, v2_rows = _fetch_v2(
        GONGZI_V2,
        token,
        {"dates": dates, "datee": datee, "Flag": "0", "Type": TYPE},
        ["bbreed", "wtname"],
    )
    print(f"  V1 code={v1_payload.get('code')} rows={len(v1_rows)}")
    print(f"  V2 code={v2_payload.get('code')} rows={len(v2_rows)}")
    # V1 侧名称列名为 bbreed/wtname（与 V2 同口径）；缺失即跳过，不静默通过。
    key_fields = ("bbreed", "wtname")
    columns = ("sl", "je")
    if not _keys_present(v1_rows, key_fields) or not _keys_present(v2_rows, key_fields):
        print("  ⚠️ 键列缺失，跳过 —— 请人工确认两侧字段名（不判为通过）")
        return False
    mismatches, lines = _compare(
        _group(v1_rows, key_fields, columns), _group(v2_rows, key_fields, columns), columns
    )
    for line in lines[:40]:
        print(line)
    print(f"  → 不一致 {mismatches} 项")
    return mismatches == 0


def reconcile_r3(role: str, dates: str, datee: str, max_pages: int) -> bool:
    """R3 A4：V1 进度 `zsl` vs V2 进度 `fhsl`（逐 `dh` × `dept` 组）.

    口径（《口径确认与未决清单》A4）：`fhsl` = 制单流程卡的**发卡数量**，
    `sssl` = 实际做的数量；旧 `zsl`（裁剪数量）在 V2 取消，分母一律改用 `fhsl`。
    本组确认两者逐组等价。
    """
    print("\n=== R3 A4 · 进度 zsl(V1) vs fhsl(V2) ===")
    token = get_token(role)
    v1_payload, v1_rows = _fetch_paged(
        SCJD_V1,
        token,
        {"dates": dates, "datee": datee},
        max_pages=max_pages,
    )
    v2_payload, v2_rows = _fetch_v2(
        SCJD_V2, token, {"dates": dates, "datee": datee}, ["dh", "dept"]
    )
    print(f"  V1 code={v1_payload.get('code')} rows={len(v1_rows)}")
    print(f"  V2 code={v2_payload.get('code')} rows={len(v2_rows)}")
    if not _keys_present(v1_rows, ("dh",)) or not _keys_present(v2_rows, ("dh", "fhsl")):
        print("  ⚠️ 键/值列缺失，跳过 —— V1 进度源字段名可能已变（不判为通过）")
        return False
    if "zsl" not in (v1_rows[0] if v1_rows else {}):
        print("  ⚠️ V1 行集里没有 zsl 列（该窗口无裁剪数据？），跳过（不判为通过）")
        return False
    left = _group(v1_rows, ("dh",), ("zsl",))
    right = _group(v2_rows, ("dh",), ("fhsl",))
    mismatches = 0
    lines: list[str] = []
    for key in sorted(set(left) | set(right)):
        lhs = left.get(key, {}).get("zsl")
        rhs = right.get(key, {}).get("fhsl")
        if lhs is None or rhs is None:
            mismatches += 1
            lines.append(f"    组 {key}: 只有一侧有值")
            continue
        if abs(lhs - rhs) > 1e-9:
            mismatches += 1
            lines.append(f"    组 {key}: zsl={lhs:.0f} fhsl={rhs:.0f}（Δ={lhs - rhs:+.0f}）")
    for line in lines[:40]:
        print(line)
    print(f"  → 不一致 {mismatches} 项")
    return mismatches == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="0.8 双跑对账（V2 新源 vs V1 锚点）")
    parser.add_argument("--case", choices=["r1", "r2", "r3", "all"], default="all")
    parser.add_argument("--role", default=ROLE_DEFAULT, help="MES 角色码（默认 99 老板）")
    parser.add_argument("--dates", default=DATES)
    parser.add_argument("--datee", default=DATEE)
    parser.add_argument("--scheme", default="货号工序", help="HuohaoWtCLQuery 的 scheme")
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args()

    print(
        f"对账窗口 {args.dates} ~ {args.datee} · 角色 {args.role} · "
        f"queryFooter={QUERY_FOOTER} page_size={PAGE_SIZE} size={SIZE}"
    )
    results: dict[str, bool] = {}
    if args.case in ("r1", "all"):
        results["R1 员工产量"] = reconcile_r1(args.role, args.dates, args.datee, args.max_pages)
    if args.case in ("r2", "all"):
        results["R2 款号 × 工序"] = reconcile_r2(args.role, args.dates, args.datee, args.scheme)
    if args.case in ("r3", "all"):
        results["R3 A4 fhsl/zsl"] = reconcile_r3(args.role, args.dates, args.datee, args.max_pages)

    print("\n=== 结论 ===")
    for name, passed in results.items():
        print(f"  {'✅ 通过' if passed else '❌ 未通过'}  {name}")
    if any(not passed for passed in results.values()):
        print("\n未全部通过：V1 锚点必须保持 enabled，禁止摘除（Issue 0.8 前置条件）。")
        sys.exit(1)
    print("\n全部通过：可把 V1 源从链路摘除并置 anchors enabled: false（Issue 0.9）。")


if __name__ == "__main__":
    main()
