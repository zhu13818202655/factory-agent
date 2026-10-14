"""跨范围缓存派生验证实验（离线一次性脚本，直连客户 MES）。

用法（仓库根目录）：

    set -a; source .env; set +a
    .venv/bin/python tools/probe_scope_derivation.py --window 2026-07
    .venv/bin/python tools/probe_scope_derivation.py --dates 2026-07-01 --datee 2026-07-31
    .venv/bin/python tools/probe_scope_derivation.py --window 2026-07 --reuse   # 只用已缓存响应
    .venv/bin/python tools/probe_scope_derivation.py --window 2026-07 --raw     # 额外落盘原始响应

实验问题：**以老板（99）视角缓存一份全厂结果集、提问时按提问者范围在本地过滤派生**，
能否复现角色原生返回的结果？

五项判据：

- **E1 列可得性**：目标粒度的响应行里是否含 ``uid`` / ``dept``（能表达范围）。
- **E2 范围包含性**：``rows(角色)`` 与 ``filter(rows(99), 范围)`` 投影到公共列后逐行全等。
- **E3 聚合等价性**：角色原生的汇总响应 vs 过滤后 99 明细的本地重聚合，逐组比对；
  比率列（``wcl``）按 ``Σsssl / Σfhsl`` 重算，浮点列用容差。
- **E4 孤儿行**：99 行集中 ``uid`` / ``dept`` 为空的行 —— 它们在窄范围视角是否存在。
- **E5 稳定性**：同一组请求重复执行，比对结果是否稳定。

范围基准一律由 MES 自己给出，不另造身份：``01`` / ``02`` 以其身份调 ``EmployeeQuery``
取可见 ``dept`` 去重集合；``00`` 取令牌里的 ``user``；``99`` 不设界。
（与 ``tools/verify_dept_scope.py`` 已建立的核对方式一致。）

脱敏（SECURITY）：控制台**默认不输出金额与姓名取值**（命中即打码），
原始响应只落 ``.cache/probe/``（该目录已被忽略）。加 ``--raw`` 才额外落盘完整响应体。
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from direct_mes_client import BASE, TIMEOUT, get_token  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = ROOT / ".cache" / "probe"
RAW_DIR = ROOT / ".cache" / "probe_raw"

GONGZI_V2 = "/api/NetYf/Sclzd/v2/GongziClQuery"
SCJD_V2 = "/api/NetYf/Sclzd/v2/ScjdQuery"
EMPLOYEE = "/api/NetYf/Baseinfo/EmployeeQuery"

ROLE_LABEL = {"00": "员工", "01": "组长", "02": "管理", "99": "老板"}

#: 控制台打码的列（Confidential）。键里的范围维度列保留，否则无法定位差异。
MASKED = frozenset({"uname", "je", "price", "mobile", "loginUserName", "name_pk"})
#: 可加度量列；``wcl`` 是比率，单独重算。
ADDITIVE = ("bs", "fhsl", "sssl", "sl", "zpsl", "wgs", "wts")
RATIO = "wcl"
#: 浮点列对账容差（实测 footer 与逐行 SUM 在浮点列上存在累加精度差）。
TOLERANCE = 1e-6

#: 调用矩阵：族 × 粒度。``groupby=None`` 表示默认明细模式。
MATRIX: list[dict[str, Any]] = [
    {"case": "M1", "family": "工资", "path": GONGZI_V2, "groupby": None, "roles": "00,01,02,99"},
    {
        "case": "M2",
        "family": "工资",
        "path": GONGZI_V2,
        "groupby": ["uid", "uname", "dept", "deptname"],
        "roles": "01,02,99",
    },
    {
        "case": "M3",
        "family": "工资",
        "path": GONGZI_V2,
        "groupby": ["dept", "deptname"],
        "roles": "01,02,99",
    },
    {"case": "M4", "family": "进度", "path": SCJD_V2, "groupby": None, "roles": "01,02,99"},
    {
        "case": "M5",
        "family": "进度",
        "path": SCJD_V2,
        "groupby": ["dh", "jhdh", "chuanghao", "huohao", "bbreed"],
        "roles": "01,02,99",
    },
    {
        "case": "M6",
        "family": "进度",
        "path": SCJD_V2,
        "groupby": ["dept", "deptname"],
        "roles": "01,02,99",
    },
    {
        "case": "M7",
        "family": "进度",
        "path": SCJD_V2,
        "groupby": ["dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"],
        "roles": "01,02,99",
    },
]

WINDOWS = {
    "工资": {"Flag": "0", "Type": "0,1,2"},
    "进度": {},
}


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def call_v2(role: str, path: str, business: dict[str, Any], case: str) -> dict[str, Any]:
    """带三参 + Bearer 调用一个 V2 接口，原始响应落 .cache/probe/。"""
    cached = PROBE_DIR / f"{case}-{role}.json"
    if CACHED_ONLY and cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    token = get_token(role)
    body = {
        "app_key": token["appkey"],
        "timestamp": token["timestamp"],
        "sign": token["sign"],
        **{k: v for k, v in business.items() if v not in (None, "", [])},
    }
    print(f"  → POST {path} [{role} {ROLE_LABEL[role]}] keys={sorted(body)}")
    resp = httpx.post(
        f"{BASE}{path}",
        json=body,
        headers={"Authorization": f"Bearer {token['accessToken']}"},
        timeout=TIMEOUT,
    )
    payload = resp.json()
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if RAW:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{case}-{role}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return payload


def rows_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """V2 实测：载荷在 result，不是文档示例写的 data。"""
    body = payload.get("result") or payload.get("data") or {}
    rows = body.get("list") if isinstance(body, dict) else None
    code, message = payload.get("code"), payload.get("message")
    if not isinstance(rows, list):
        raise RuntimeError(f"未识别载荷：code={code} message={message!r}")
    if code not in (1, 200):
        raise RuntimeError(f"非成功码：code={code} message={message!r}")
    return rows


def visible_depts(role: str) -> set[str]:
    """该角色的可见部门集，由 MES 自己给出。"""
    if role == "99":
        return set()
    payload = call_v2(role, EMPLOYEE, {}, f"dept-{role}")
    rows = rows_of(payload)
    return {str(r.get("dept")) for r in rows if r.get("dept")}


def own_uid(role: str) -> str:
    return str(get_token(role).get("user") or "")


def in_scope(row: dict[str, Any], role: str, depts: set[str]) -> bool:
    if role == "99":
        return True
    if role == "00":
        return str(row.get("uid") or "") == OWN_UID
    return str(row.get("dept") or "") in depts


# ---------------------------------------------------------------------------
# 比对工具
# ---------------------------------------------------------------------------
def format_key(cols: list[str], key: tuple) -> str:
    """把比较键渲染成可读串，Confidential 列打码。"""
    return " ".join(
        f"{col}=<masked>" if col in MASKED else f"{col}={value}"
        for col, value in zip(cols, key, strict=False)
    )


def key_of(row: dict[str, Any], cols: list[str]) -> tuple:
    return tuple(json.dumps(row.get(c), ensure_ascii=False) for c in cols)


def common_cols(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[str]:
    if not a or not b:
        return []
    ca, cb = set(a[0]), set(b[0])
    return [c for c in a[0] if c in cb and c in ca]


def compare_rows(
    native: list[dict[str, Any]], derived: list[dict[str, Any]], cols: list[str]
) -> dict[str, Any]:
    """以公共列上的多重集比对两组行。"""
    na = Counter(key_of(r, cols) for r in native)
    de = Counter(key_of(r, cols) for r in derived)
    return {
        "native": len(native),
        "derived": len(derived),
        "only_native": sum((na - de).values()),
        "only_derived": sum((de - na).values()),
        "matched": sum((na & de).values()),
        "samples_only_native": [k for k, _ in list((na - de).items())[:5]],
        "samples_only_derived": [k for k, _ in list((de - na).items())[:5]],
    }


def aggregate(rows: list[dict[str, Any]], group_cols: list[str]) -> dict[tuple, dict[str, float]]:
    """按分组列重聚合：可加列求和，比率列按 Σsssl / Σfhsl 重算。"""
    buckets: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        key = key_of(row, group_cols)
        bucket = buckets[key]
        for col in ADDITIVE:
            value = row.get(col)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket[col] += float(value)
    for bucket in buckets.values():
        fhsl, sssl = bucket.get("fhsl", 0.0), bucket.get("sssl", 0.0)
        if fhsl:
            bucket[RATIO] = round(sssl / fhsl * 100, 2)
    return dict(buckets)


def compare_aggregates(
    native_rows: list[dict[str, Any]], derived_rows: list[dict[str, Any]], group_cols: list[str]
) -> dict[str, Any]:
    native = aggregate(native_rows, group_cols)
    derived = aggregate(derived_rows, group_cols)
    keys = set(native) | set(derived)
    missing = sorted(keys - set(native))
    extra = sorted(keys - set(derived))
    mismatched: list[tuple] = []
    for key in sorted(keys & set(native) & set(derived)):
        for col, value in native[key].items():
            other = derived[key].get(col, 0.0)
            if abs(value - other) > max(TOLERANCE, abs(value) * 1e-9):
                mismatched.append((key, col, value, other))
                break
    return {
        "native_groups": len(native),
        "derived_groups": len(derived),
        "groups_missing_in_derived": len(missing),
        "groups_extra_in_derived": len(extra),
        "groups_with_value_mismatch": len(mismatched),
        "sample_mismatch": [
            f"{format_key(group_cols, k)}  {c}: 原生 {n} vs 派生 {d}"
            for k, c, n, d in mismatched[:5]
        ],
        "equal": not missing and not extra and not mismatched,
    }


# ---------------------------------------------------------------------------
# 各判据
# ---------------------------------------------------------------------------
def e1_columns(case: dict[str, Any], rows99: list[dict[str, Any]]) -> dict[str, Any]:
    cols = set(rows99[0]) if rows99 else set()
    present = sorted({"uid", "uname", "dept", "deptname"} & cols)
    return {
        "case": case["case"],
        "family": case["family"],
        "granularity": "明细（默认列）" if case["groupby"] is None else "汇总",
        "groupby": case["groupby"],
        "rows": len(rows99),
        "scope_columns_present": present,
        "filterable": {"dept", "uid"} & cols != set(),
    }


def e4_orphans(rows99: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows99)
    no_uid = [r for r in rows99 if not r.get("uid")]
    no_dept = [r for r in rows99 if not r.get("dept")]
    return {
        "rows": total,
        "uid_empty": len(no_uid),
        "uid_empty_pct": round(len(no_uid) / total * 100, 1) if total else 0.0,
        "dept_empty": len(no_dept),
        "dept_empty_pct": round(len(no_dept) / total * 100, 1) if total else 0.0,
    }


def stability(rows99: list[dict[str, Any]], prior: list[dict[str, Any]] | None) -> str:
    if prior is None:
        return "首次运行，无对照（重复运行时自动比对）"
    cols = common_cols(rows99, prior)
    diff = compare_rows(prior, rows99, cols)
    if diff["only_native"] or diff["only_derived"]:
        return f"不稳定：两次行集不同（-{diff['only_native']} / +{diff['only_derived']}）"
    return "稳定：两次行集一致"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_case(case: dict[str, Any], dates: str, datee: str) -> dict[str, Any]:
    print("\n" + "=" * 92)
    print(f"[{case['case']}] {case['family']} · {case['groupby'] or '明细默认列'}")
    business: dict[str, Any] = {"dates": dates, "datee": datee, **WINDOWS[case["family"]]}
    if case["groupby"]:
        business["groupbyFields"] = case["groupby"]

    roles = case["roles"].split(",")
    payloads: dict[str, dict[str, Any]] = {}
    for role in roles:
        try:
            payloads[role] = call_v2(role, case["path"], business, case["case"])
        except SystemExit as exc:  # 缺凭据
            print(f"    跳过 {role}：{exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"    跳过 {role}：{exc}")

    result: dict[str, Any] = {"case": case["case"]}
    if "99" not in payloads:
        result["skipped"] = "缺少 99 基准响应，无法比对"
        print(f"    {result['skipped']}")
        return result

    rows99 = rows_of(payloads["99"])
    result["e1"] = e1_columns(case, rows99)
    print(
        f"  E1 列可得性      : 范围列 {result['e1']['scope_columns_present'] or '无'}"
        f" → {'可过滤' if result['e1']['filterable'] else '不可过滤'}"
    )
    result["e4"] = e4_orphans(rows99)
    print(
        f"  E4 孤儿行        : uid 空 {result['e4']['uid_empty']}"
        f"({result['e4']['uid_empty_pct']}%)  dept 空 {result['e4']['dept_empty']}"
        f"({result['e4']['dept_empty_pct']}%)"
    )

    for role in roles:
        if role == "99" or role not in payloads:
            continue
        try:
            rows_role = rows_of(payloads[role])
        except Exception as exc:  # noqa: BLE001
            print(f"  [{role}] 原生响应不可用：{exc}")
            continue
        depts = visible_depts(role) if role != "00" else set()
        derived = [r for r in rows99 if in_scope(r, role, depts)]
        cols = common_cols(rows_role, derived)
        e2 = compare_rows(rows_role, derived, cols)
        print(
            f"  E2 [{role} {ROLE_LABEL[role]}] 范围列 {len(cols)}  原生 {e2['native']} / "
            f"派生 {e2['derived']}  匹配 {e2['matched']}  "
            f"仅原生 {e2['only_native']}  仅派生 {e2['only_derived']}  "
            f"→ {'全等 ✓' if not e2['only_native'] and not e2['only_derived'] else '有差异 ✗'}"
        )
        if e2["samples_only_native"] or e2["samples_only_derived"]:
            for key in e2["samples_only_native"][:2]:
                print(f"      仅原生: {format_key(cols, key)}")
            for key in e2["samples_only_derived"][:2]:
                print(f"      仅派生: {format_key(cols, key)}")
        result.setdefault("e2", {})[role] = e2

        if case["groupby"]:
            e3 = compare_aggregates(rows_role, derived, case["groupby"])
            print(
                f"  E3 [{role} {ROLE_LABEL[role]}] 分组 原生 {e3['native_groups']} / "
                f"派生 {e3['derived_groups']}  缺组 {e3['groups_missing_in_derived']}  "
                f"多组 {e3['groups_extra_in_derived']}  值不符 {e3['groups_with_value_mismatch']}"
                f"  → {'全等 ✓' if e3['equal'] else '有差异 ✗'}"
            )
            for sample in e3["sample_mismatch"]:
                print(f"      {sample}")
            result.setdefault("e3", {})[role] = e3

    prior_path = PROBE_DIR / f"prev-{case['case']}-99.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else None
    result["e5"] = stability(rows99, rows_of(prior) if prior else None)
    print(f"  E5 稳定性        : {result['e5']}")
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    prev_payload = {"result": {"list": rows99}}
    prior_path.write_text(json.dumps(prev_payload, ensure_ascii=False), encoding="utf-8")
    return result


def check_settled(dates: str, datee: str) -> None:
    if date.fromisoformat(datee) >= date.today():
        print(
            "!! 窗口未结算（结束日不早于今天）。数据仍在变化，比对结果可能是假差异。\n"
            "   建议使用已结算的自然窗口，例如 --window 2026-07。"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="跨范围缓存派生验证实验")
    parser.add_argument("--window", help="已结算月份，形如 2026-07")
    parser.add_argument("--dates", help="开始日期 yyyy-MM-dd")
    parser.add_argument("--datee", help="结束日期 yyyy-MM-dd")
    parser.add_argument("--reuse", action="store_true", help="只用 .cache/probe 里已缓存的响应")
    parser.add_argument("--raw", action="store_true", help="额外落盘完整原始响应体")
    parser.add_argument("--case", help="只跑指定用例（M1..M7）")
    args = parser.parse_args()

    if args.window:
        year, month = args.window.split("-")
        last = (date(int(year) + (int(month) == 12), int(month) % 12 + 1, 1) - date.resolution).day
        dates, datee = f"{args.window}-01", f"{args.window}-{last:02d}"
    elif args.dates and args.datee:
        dates, datee = args.dates, args.datee
    else:
        parser.error("需要 --window 或 --dates/--datee")

    global CACHED_ONLY, OWN_UID, RAW
    CACHED_ONLY, RAW = args.reuse, args.raw
    check_settled(dates, datee)

    selected = [c for c in MATRIX if not args.case or c["case"] == args.case]
    print(f"窗口 {dates} ~ {datee}   用例 {[c['case'] for c in selected]}")
    print("范围基准由 MES 自己给出（01/02 取 EmployeeQuery 可见部门；00 取令牌 user；99 不设界）")

    OWN_UID = own_uid("00") if any("00" in c["roles"] for c in selected) else ""
    results = [run_case(case, dates, datee) for case in selected]

    report = ROOT / ".cache" / f"probe-report-{dates}_{datee}.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 92)
    print(f"报告已写入 {report}（脱敏：金额与姓名列未输出）")
    print("结论判读见 docs/design/跨范围缓存派生验证实验.md §四。")
    return 0


CACHED_ONLY = False
RAW = False
OWN_UID = ""

if __name__ == "__main__":
    sys.exit(main())
