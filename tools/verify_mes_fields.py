"""用客户真实响应批次校验《AI问答对外接口-整理.md》的字段表与字段关联（离线，不联网）。

用法（仓库根目录）：
    uv run tools/verify_mes_fields.py                      # 读 cache/ 下所有 .json
    uv run tools/verify_mes_fields.py cache/生产工序.json   # 只校验指定文件

文件按文件名识别接口（含 HuohaoForm / 货号信息表单 → 表单接口等关键字），识别不到就只做通用字段统计。
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"

# 文档（docs/product/AI问答对外接口-整理.md）当前声明的字段
DOC_FIELDS: dict[str, dict[str, list[str]]] = {
    "HuohaoQuery": {
        "huohaoList": [
            "bh", "bbreed", "name_pk", "description", "stype", "huohaotype",
            "dw", "lpinpai", "isdelete", "jst_huohao",
        ],
        "huohaoTypeList": ["id", "bh", "pbh", "name", "name_pk", "isdelete"],
    },
    "HuohaoFormQuery": {
        "huohaoList": [
            "bh", "bbreed", "name_pk", "description", "stype", "huohaotype",
            "dw", "lpinpai", "isdelete", "jst_huohao",
        ],
        "huohaoColorList": ["id", "bh", "color", "uploadguid"],
        "huohaoChimaList": ["id", "bh", "chima", "banx", "kez", "xs_price", "price"],
    },
    "ScTypeQuery": {"sctypeList": ["bh", "name", "name_pk", "sfjcj", "isdelete", "sfcprk"]},
    "RfidWorktypeQuery": {
        "worktypeList": [
            "bh", "name", "name_pk", "gxtype", "isdelete", "section", "jc", "sc_type",
            "worktype_group", "yfgs", "default_price", "gongzi_js_type", "wt_sort",
            "xz_price", "default_working_hours", "vehicle_type",
        ]
    },
    "EmployeeQuery": {
        "employeeList": [
            "uid", "uname", "name_pk", "mobile", "move_Login", "dept", "deptname",
            "move_scan", "loginUserName", "zr_ck", "dy_gongzhong", "roles",
        ]
    },
    "DeptQuery": {"deptList": ["bh", "name", "pid", "isdelete", "sysdept", "sorde"]},
    "UserInfoQuery": {"list": ["code", "username", "realname", "companyName", "roles"]},
}

FILE_HINTS = (
    ("货号信息表单", "HuohaoFormQuery"),
    ("货号工序", "HuohaoWorktypeQuery"),
    ("货号信息单据", "HuohaoQuery"),
    ("生产类型", "ScTypeQuery"),
    ("生产工序", "RfidWorktypeQuery"),
    ("员工信息", "EmployeeQuery"),
    ("部门信息", "DeptQuery"),
    ("用户信息", "UserInfoQuery"),
)


def endpoint_of(path: Path) -> str | None:
    for hint, name in FILE_HINTS:
        if hint in path.name:
            return name
    return None


def keyset(rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        out |= set(row)
    return out


def dist(rows: list[dict[str, Any]], field: str, top: int = 5) -> str:
    counter = Counter(json.dumps(r.get(field), ensure_ascii=False) for r in rows)
    parts = ", ".join(f"{value}×{count}" for value, count in counter.most_common(top))
    return f"{len(counter)} 种 | {parts}" + ("…" if len(counter) > top else "")


def report_array(label: str, rows: list[dict[str, Any]], doc_fields: list[str] | None) -> None:
    real = keyset(rows)
    print(f"\n  [{label}] {len(rows)} 条")
    if doc_fields is not None:
        doc = set(doc_fields)
        print(f"    文档有、真实无（过时字段）: {sorted(doc - real) or '无'}")
        print(f"    真实有、文档无（缺登记）: {sorted(real - doc) or '无'}")
    for field in sorted(real):
        values = [r.get(field) for r in rows]
        types = Counter(type(v).__name__ for v in values)
        nulls = sum(1 for v in values if v is None)
        sample = next((v for v in values if v not in (None, "", 0)), None)
        print(f"    {field:<22} 类型={dict(types)} 空={nulls} 取值={dist(rows, field)} 样例={sample!r}")
    if len(rows) and all(isinstance(r, dict) for r in rows):
        uniq = {f: len({json.dumps(r.get(f), ensure_ascii=False) for r in rows}) for f in sorted(real)}
        print(f"    唯一值数: {uniq}")


def analyze(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = payload.get("result")
    endpoint = endpoint_of(path)
    print("=" * 88)
    print(f"{path.name}  接口={endpoint or '未识别'}  code={payload.get('code')} message={payload.get('message')!r}")
    if not isinstance(result, dict):
        print("  result 非对象：", repr(result)[:200])
        return
    doc = DOC_FIELDS.get(endpoint or "", {})
    print("  result 键:", {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()})
    for key, value in result.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            report_array(f"result.{key}", value, doc.get(key))

    # ---- 关联抽检 ----
    hh = result.get("huohaoList")
    if isinstance(hh, list) and hh and isinstance(hh[0], dict):
        pair = {(x.get("bh"), x.get("bbreed")) for x in hh}
        print("\n  [关联] bh 唯一:", len({x.get('bh') for x in hh}) == len(hh))
        print("  [关联] bbreed 唯一:", len({x.get('bbreed') for x in hh}) == len(hh))
        print("  [关联] bh↔bbreed 1:1:", len(pair) == len(hh))
        tl = result.get("huohaoTypeList")
        if isinstance(tl, list) and tl:
            type_bh = {t.get("bh") for t in tl}
            type_name = {t.get("bh"): t.get("name") for t in tl}
            stypes = {x.get("stype") for x in hh}
            print("  [关联] stype ⊆ huohaoTypeList.bh:", stypes <= type_bh,
                  "| 未匹配:", sorted(stypes - type_bh) or "无")
            print("  [关联] huohaoTypeList.bh 未被引用:", sorted(type_bh - stypes) or "无")
            print("  [关联] stype→huohaotype 唯一:",
                  all(len(v) == 1 for v in defaultdict(set, {s: {x['huohaotype'] for x in hh if x.get('stype') == s} for s in stypes}).values()))
            print("  [关联] huohaotype == 类型表 name:",
                  sum(1 for x in hh if type_name.get(x.get("stype")) == x.get("huohaotype")), "/", len(hh))

    worktype = result.get("worktypeList")
    if isinstance(worktype, list) and worktype and isinstance(worktype[0], dict):
        sections = {x.get("section") for x in worktype}
        print("\n  [关联] worktypeList.section 取值:", sorted(sections))
        types = result.get("sctypeList")
        if isinstance(types, list) and types:
            names = {t.get("name") for t in types}
            print("  [关联] section ⊆ sctypeList.name:", sections <= names,
                  "| 未匹配:", sorted(sections - names) or "无")


def main() -> None:
    args = sys.argv[1:]
    files = [Path(a) for a in args] if args else sorted(CACHE.glob("*.json"))
    if not files:
        print(f"未找到响应文件（默认目录 {CACHE}）。先用调试台「保存结果」落盘，或传入文件路径。")
        return
    for path in files:
        if not path.exists():
            print(f"跳过（不存在）: {path}")
            continue
        analyze(path)
    print("\n" + "=" * 88)
    print(f"共校验 {len(files)} 个文件。")


if __name__ == "__main__":
    main()
