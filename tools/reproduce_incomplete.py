"""复现 factory-agent 各种 `incomplete_reason` 的 MES 侧原始条件（直连，不经 8000）。

用法:
    set -a; source .env; set +a
    python tools/reproduce_incomplete.py            # 跑全部四类
    python tools/reproduce_incomplete.py pagination # 只跑某一类
    python tools/reproduce_incomplete.py --role 00  # 指定角色跑全部

每个场景都会标注：对应 factory-agent 的 incomplete_reason、使用的角色与底层接口函数。

对照关系：
  1) pagination_*  -> 历史 wrapped 错误形态（服务端忽略分页→翻页重复）已于
     2026-09-06 随“全族一律 flat”退役；本场景改为验证 flat 下分页不重复。
  2) metric_unavailable:* -> FR-001 defective_qty 列无数据源（MES 行里没有次品字段）
  3) reconciliation_failed -> 明细 sum(je) 与 footer.je_total 不一致（截断取数演示）
  4) upstream_invalid    -> MES 返回 code=0 业务错误（queryFooter 传字符串被拒）
"""

import argparse
from collections.abc import Callable
from decimal import Decimal

from direct_mes_client import _call, _uid, get_token

DATES, DATEE = "2026-07-01", "2026-07-31"
SEPT = ("2026-09-01", "2026-09-30")


# ---------------------------------------------------------------------------
# 1) 分页不完整：wrapped 错误形态 → 固定首页 + 恒定 total → duplicate_page
# ---------------------------------------------------------------------------
def reproduce_pagination_duplicate(role: str = "99") -> dict:
    """历史 wrapped 错误形态已退役（2026-09-06 起全族 flat，服务端不再忽略分页）。
    此处用 flat 请求验证第 1 页与第 2 页内容不同，不再触发 pagination_duplicate_page。"""
    token = get_token(role)
    print(
        f"\n[1] 分页不完整 pagination_duplicate_page（flat 下不可复现）  —— 角色 {role}，"
        f"接口 GongziMxQuery（flat 形态，全族平铺）"
    )
    seen = []
    for page in (1, 2):
        payload = _call(
            "/api/NetYf/Sclzd/GongziMxQuery",
            token,
            {
                "page": 1,
                "size": 1081,
                "queryFooter": False,
                "Flag": "1",
                "Type": "2",
                "scheme": "",
                "Uid": _uid(token),
                "dates": SEPT[0],
                "datee": SEPT[1],
            },
            bool_params=("queryFooter",),
        )
        # 保存 repsonse 为 JSON 文件 以便人工比对
        import json

        with open(f"reproduce_pagination_{role}_page{page}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        rows = (payload.get("result") or {}).get("list") or []
        ids = tuple(str(r.get("id")) for r in rows)
        seen.append(ids)
        total = (payload.get("result") or {}).get("total")
        first = rows[0].get("id") if rows else None
        print(
            f"  page={page}: code={payload.get('code')} total={total} "
            f"rows={len(rows)} first_id={first}"
        )
    same = bool(seen[0]) and seen[0] == seen[1]
    print(
        f"  -> 第1页与第2页内容{'相同' if same else '不同'}；若相同，factory-agent 会给出"
        f" 不完整原因 pagination_duplicate_page（前端文案：分页未取全：服务端忽略分页参数）"
    )
    return {"page1_equals_page2": same}


# ---------------------------------------------------------------------------
# 2) 指标无源 metric_unavailable:quality_defective：FR-001 次品列
# ---------------------------------------------------------------------------
def reproduce_metric_unavailable(role: str = "00") -> dict:
    """FR-001 个人产量里 defective_qty(次品数) 无数据源。
    MES GongziMxQuery 明细行里没有“次品”字段，因此该列被设计为 unavailable。"""
    print(
        f"\n[2] 指标无源 metric_unavailable:quality_defective  —— 角色 {role}，"
        f"接口 GongziMxQuery（明细），对应能力 FR-001 的 defective_qty 列"
    )
    # 直接复用 direct_mes_client.gongzi_mx_query 同款 flat 明细请求
    from direct_mes_client import gongzi_mx_query

    payload = gongzi_mx_query(role, scheme="", dates=DATES, datee=DATEE)
    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        print(f"  本角色 {DATES}~{DATEE} 无明细行，无法展示字段集合（可换 8 月窗口重试）。")
        return {"has_rows": False}
    keys = sorted({str(k) for row in rows for k in row.keys()})
    defect_keys = [k for k in keys if "次品" in k or k.lower() in {"defective", "defective_qty"}]
    print(f"  返回行字段 keys：{keys}")
    print(f"  其中疑似次品字段：{defect_keys or '无'}")
    print(
        f"  -> MES 行里{'没有' if not defect_keys else '有'}次品字段，所以 "
        f"FR-001 的 defective_qty 列"
        f" 在 8000 卡片上会显示 unavailable + reason=metric_unavailable:quality_defective。"
        f" 这不是上游报错，是产品口径里该指标没有可逐行归属的数据源。"
    )
    return {"has_defect_column": bool(defect_keys), "row_keys": keys}


# ---------------------------------------------------------------------------
# 3) 对账失败 reconciliation_failed：明细 sum(je) vs footer.je_total
# ---------------------------------------------------------------------------
def _je_sum(payload: dict) -> tuple[Decimal, int, dict | None]:
    res = payload.get("result") or {}
    rows = res.get("list") or []
    total = Decimal("0")
    for row in rows:
        try:
            total += Decimal(str(row.get("je", 0) or 0))
        except Exception:  # noqa: BLE001
            continue
    return total, len(rows), res.get("footer")


def _equals_footer(local: Decimal, footer_raw: object) -> bool:
    """数值比较（Decimal 忽略尾零），与 factory-agent 对账逻辑一致。"""
    try:
        return local == Decimal(str(footer_raw))
    except Exception:  # noqa: BLE001
        return False


def reproduce_reconciliation(role: str = "00") -> dict:
    """明细行合计 vs MES footer.je_total。取全一致则不会触发；
    人为“只取前几条”即可复现不一致 → factory-agent 判 reconciliation_failed。"""
    print(f"\n[3] 对账失败 reconciliation_failed  —— 角色 {role}，接口 GongziMxQuery（明细，flat）")
    token = get_token(role)
    from direct_mes_client import gongzi_mx_query

    full = gongzi_mx_query(role, scheme="", dates=DATES, datee=DATEE)
    je_sum, n, footer = _je_sum(full)
    footer_je = footer.get("je_total") if footer else None
    ok = _equals_footer(je_sum, footer_je)
    print(f"  取全({n}行)：sum(je)={je_sum}  footer.je_total={footer_je}  一致? {ok}")

    # 人为截断：只取前 5 行 → 本地合计必然 < footer
    truncated = _call(
        "/api/NetYf/Sclzd/GongziMxQuery",
        token,
        {
            "page": 1,
            "size": 5,
            "queryFooter": True,
            "Flag": "0",
            "Type": "0,1,2",
            "scheme": "",
            "Uid": _uid(token),
            "dates": DATES,
            "datee": DATEE,
        },
        bool_params=("queryFooter",),
    )
    part_sum, part_n, _ = _je_sum(truncated)
    match = _equals_footer(part_sum, footer_je)
    print(f"  截断只取 {part_n} 行：sum(je)={part_sum}  footer.je_total={footer_je}  一致? {match}")
    if not match:
        print(
            "  -> 这就是 reconciliation_failed 的触发条件：本地拿到的行合计 ≠ MES footer 合计，"
            " 谁对谁错不确定，factory-agent 宁可标对账失败也不取一侧。"
        )
    return {"full_match": _equals_footer(je_sum, footer_je), "truncated_mismatch": not match}


# ---------------------------------------------------------------------------
# 4) 上游错误 upstream_invalid：queryFooter 传字符串被整包拒绝
# ---------------------------------------------------------------------------
def reproduce_upstream_invalid(role: str = "00") -> dict:
    """历史中最典型的上游错误：09-06 员工 00 + GongziMxQuery，
    queryFooter 传字符串("1") → MES 整包回 code=0 请求参数缺少app_key、timestamp、sign
    → factory-agent 降级为 incomplete_reason=upstream_invalid。"""
    print(
        f"\n[4] 上游错误 upstream_invalid  —— 角色 {role}，接口 GongziMxQuery（flat），"
        f"把 queryFooter 故意传成字符串"
    )
    token = get_token(role)
    payload = _call(
        "/api/NetYf/Sclzd/GongziMxQuery",
        token,
        {
            "page": 1,
            "size": 200,
            "queryFooter": "1",
            "Flag": "0",
            "Type": "0,1,2",
            "scheme": "",
            "Uid": _uid(token),
            "dates": DATES,
            "datee": DATEE,
        },
        bool_params=(),  # 不转布尔，保留字符串形态
    )
    print(f"  code={payload.get('code')} message={payload.get('message')!r}")
    if payload.get("code") != 1:
        print(
            "  -> MES 返回业务错误 → factory-agent 会记日志 mes.upstream.rejected 并降级为"
            " 不完整原因 upstream_invalid（不整次失败）。"
        )
    else:
        print("  -> 该形态当前未复现拒绝（已修复/租户不同），可换角色再试。")
    return {"code": payload.get("code"), "message": payload.get("message")}


SECTIONS: dict[str, Callable[[str], dict]] = {
    "pagination": reproduce_pagination_duplicate,
    "metric": reproduce_metric_unavailable,
    "reconciliation": reproduce_reconciliation,
    "upstream": reproduce_upstream_invalid,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="复现 incomplete_reason 的 MES 侧条件")
    parser.add_argument("--section", nargs="?", default="all", choices=[*SECTIONS, "all"])
    parser.add_argument("--role", default="00", choices=["00", "01", "02", "99"])
    args = parser.parse_args()
    if args.section == "all":
        order = ("pagination", "metric", "reconciliation", "upstream")
        for name in order:
            SECTIONS[name](args.role)
    else:
        SECTIONS[args.section](args.role)
    print(
        "\n完成。对照：pagination_* 分页没取全 / metric_unavailable:* 列无数据源 / "
        "reconciliation_failed 对账不一致 / upstream_* 上游报错降级。"
    )


if __name__ == "__main__":
    main()
