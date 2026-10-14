"""离线剖析已落盘的 MES V2 响应（不联网）。

用法（仓库根目录）：
    .venv/bin/python tools/analyze_v2_response.py                      # 扫 .cache/*.json
    .venv/bin/python tools/analyze_v2_response.py .cache/缝制生产进度V2.json

回答四类落地前必须确认的问题：

1. **响应封装与成功码** —— 载荷在 ``result`` 还是 ``data``？``code`` 取值？是否带
   ``message`` / ``timestamp``？这决定 ``MesEnvelope`` 是否需要改造。
2. **聚合列名与类型** —— 每列的真实类型、空值数，以及 ``bs`` / ``sl`` / ``je`` /
   ``wcl`` / ``zpsl`` 等列是否如文档所述。
3. **``footer`` 的可加性** —— ``{列}_total`` 是否等于该列的逐行 ``SUM``。相等才说明
   footer 可作为三方对账的独立校验值；不等则说明该列不可加（如整单数量被逐行重复携带），
   **绝不能用作分母或合计**。
4. **维度基数与空值占比** —— ``uid`` / ``dept`` / ``dh`` / ``jhdh`` 的唯一值数与空值率，
   用于判断该结果集能否支撑按范围本地过滤，以及订单维度按 ``jhdh`` 归组的实际形态。

脱敏约定（SECURITY）：**只输出结构信息与布尔判断**。金额、姓名、员工号、部门号等业务值
一律不打印；数值只以「是否相等」「相对偏差」「计数」形式呈现。
"""

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"

#: 当作「可加度量」检查 footer 一致性的列（金额列只报偏差，不报数值）。
ADDITIVE = ("bs", "fhsl", "sssl", "zpsl", "wgs", "wts", "sl", "je", "price")
#: 敏感列：命中时只报计数，绝不输出取值。
SENSITIVE = frozenset({"uname", "je", "price"})
#: 用于判断能否本地范围过滤的维度列。
SCOPE_KEYS = ("uid", "uname", "dept", "deptname", "dh", "jhdh")
#: 窗口字段候选（取第一个存在的）。
WINDOW_KEYS = ("inputdate", "rq", "check_time")


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return type(value).__name__


def _sum_column(rows: list[dict[str, Any]], column: str) -> float | None:
    total = 0.0
    seen = False
    for row in rows:
        value = row.get(column)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += float(value)
        seen = True
    return total if seen else None


def analyze_envelope(payload: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    keys = list(payload)
    container = "result" if "result" in payload else ("data" if "data" in payload else None)
    print(f"  顶层键        : {keys}")
    print(f"  载荷容器      : {container!r}   （文档示例写的是 data）")
    print(f"  code          : {payload.get('code')!r}")
    print(f"  message 键    : {'message' in payload or 'msg' in payload}")
    print(f"  timestamp 键  : {'timestamp' in payload}")
    if container is None:
        print("  !! 未识别载荷容器，后续分析跳过")
        return
    body = payload[container]
    if isinstance(body, dict):
        print(f"  {container} 键      : {list(body)}")
        print(f"  {container}.total    : {body.get('total')!r}   实际 list 长度: {len(rows)}")
        print(f"  footer 键     : {list(body.get('footer') or {})}")


def analyze_columns(rows: list[dict[str, Any]]) -> None:
    columns: list[str] = []
    for row in rows[:200]:
        for key in row:
            if key not in columns:
                columns.append(key)
    print(f"\n  列数 = {len(columns)}（按首次出现顺序）")
    print(f"  {'列名':<16}{'类型分布':<26}{'空值数':>8}{'空值率':>9}{'唯一值':>8}")
    total = len(rows)
    for column in columns:
        types: Counter[str] = Counter()
        empties = 0
        distinct: set[str] = set()
        for row in rows:
            value = row.get(column)
            types[_type_name(value)] += 1
            if _is_empty(value):
                empties += 1
            distinct.add(json.dumps(value, ensure_ascii=False))
        type_text = ",".join(f"{k}×{v}" for k, v in types.most_common())
        rate = f"{empties / total * 100:.1f}%" if total else "-"
        print(f"  {column:<16}{type_text:<26}{empties:>8}{rate:>9}{len(distinct):>8}")


def analyze_footer(rows: list[dict[str, Any]], footer: dict[str, Any]) -> None:
    print("\n  footer 可加性（{列}_total 是否 == 逐行 SUM）")
    for key, value in footer.items():
        column = key[:-6] if key.endswith("_total") else key
        computed = _sum_column(rows, column)
        if computed is None:
            print(f"    {key:<16} footer={value!r:<14} 逐行无该数值列 → 不可比")
            continue
        if not isinstance(value, (int, float)):
            print(f"    {key:<16} footer 非数值 → 不可比")
            continue
        if value == 0:
            flag = "相等" if computed == 0 else "不等"
            print(f"    {key:<16} footer 为 0，逐行和为 {computed:.0f} → {flag}")
            continue
        diff = abs(computed - float(value)) / abs(float(value))
        flag = "相等 ✓" if computed == float(value) else f"不等 ✗（相对偏差 {diff * 100:.4f}%）"
        print(f"    {key:<16} 逐行 SUM 与 footer {flag}")


def analyze_dimensions(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    print("\n  维度基数与空值率（判断能否本地范围过滤）")
    for key in SCOPE_KEYS:
        if key not in rows[0]:
            print(f"    {key:<10} 该列不存在")
            continue
        distinct: set[str] = set()
        empties = 0
        for row in rows:
            value = row.get(key)
            if _is_empty(value):
                empties += 1
            distinct.add(json.dumps(value, ensure_ascii=False))
        rate = f"{empties / total * 100:.1f}%" if total else "-"
        print(f"    {key:<10} 唯一值 {len(distinct):>6}   空值 {empties:>6} ({rate})")
    present = [key for key in SCOPE_KEYS if key in rows[0]]
    verdict = "可本地过滤" if {"dept", "uid"} & set(present) else "不可本地过滤（缺 dept/uid）"
    print(f"    → 可过滤判定：{verdict}（存在的范围维度列：{present}）")


def analyze_window(rows: list[dict[str, Any]]) -> None:
    for key in WINDOW_KEYS:
        if key not in rows[0]:
            continue
        values = sorted(str(row.get(key)) for row in rows if not _is_empty(row.get(key)))
        if values:
            span = f"{values[0]} ~ {values[-1]}"
            print(f"\n  窗口（列 {key}）: {span}   有值行 {len(values)}/{len(rows)}")
        return
    print("\n  窗口：默认列里没有日期列，无法判定")


def analyze_ratio_column(rows: list[dict[str, Any]], column: str = "wcl") -> None:
    if column not in rows[0]:
        return
    values = [row.get(column) for row in rows]
    numeric = [float(v) for v in values if isinstance(v, (int, float))]
    if not numeric:
        return
    hundred = sum(1 for v in numeric if v == 100)
    print(
        f"\n  {column} 分布: 有值 {len(numeric)}/{len(rows)}，"
        f"等于 100 的 {hundred} 行（{hundred / len(numeric) * 100:.1f}%），"
        f"最小 {min(numeric):.2f}，最大 {max(numeric):.2f}"
    )


def analyze(path: Path) -> None:
    size_mb = path.stat().st_size / 1024 / 1024
    print("=" * 92)
    print(f"{path.name}   {size_mb:.1f} MB")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("  顶层不是对象，跳过")
        return
    container = "result" if "result" in payload else "data"
    body = payload.get(container) or {}
    rows = body.get("list") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        print(f"  {container}.list 不是数组，跳过")
        return
    row_mb = size_mb / len(rows) * 1024 if rows else 0
    print(f"  约 {row_mb:.0f} KB/千行  |  单行约 {size_mb * 1024 * 1024 / len(rows):.0f} 字节")
    analyze_envelope(payload, rows)
    analyze_columns(rows)
    analyze_footer(rows, body.get("footer") or {})
    analyze_dimensions(rows)
    analyze_window(rows)
    analyze_ratio_column(rows)


def main() -> int:
    args = sys.argv[1:]
    files = [Path(a) for a in args] if args else sorted(CACHE.glob("*.json"))
    if not files:
        print(f"未找到响应文件（默认目录 {CACHE}）。请先用调试台「保存结果」落盘。")
        return 2
    for path in files:
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        analyze(path)
    print("\n" + "=" * 92)
    print(f"共分析 {len(files)} 个文件。业务取值一律未输出（脱敏）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
