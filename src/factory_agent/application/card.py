"""Structured result card and deterministic natural-language summary.

The card and the natural-language summary may only reference numbers that exist
in the ``RenderTable`` (or its totals). No number is ever fabricated here; any
assumption is surfaced as a warning marker, and an ``incomplete`` result is
labelled explicitly rather than presented as complete.
"""

from __future__ import annotations

from decimal import Decimal

from factory_agent.ports.contracts import UNAVAILABLE_VALUE, RenderColumn, RenderTable

_MONEY_UNITS = ("元", "¥")
_UNAVAILABLE_LABEL = "暂无数据源"


def build_card(table: RenderTable, *, title: str | None = None) -> dict[str, object]:
    """Return a structured card payload derived only from the RenderTable."""
    columns = [
        {
            "name": column.name,
            "type": column.column_type,
            "unit": column.unit,
            "value": _column_card_value(column, table),
        }
        for column in table.columns
    ]
    return {
        "kind": "result_card",
        "capability_id": table.capability_id,
        "title": title or table.capability_id,
        "columns": columns,
        "totals": {name: _decimal_str(value) for name, value in table.totals.items()},
        "row_count": len(table.rows),
        "warnings": list(table.warnings),
        "incomplete": table.incomplete,
        "incomplete_reason": table.incomplete_reason,
        "source_operations": list(table.source_operations),
        "summary": build_summary(table),
    }


def build_summary(table: RenderTable) -> str:
    """A deterministic sentence that only cites numbers already in the table."""
    parts: list[str] = []
    if table.incomplete:
        parts.append("本次结果不完整")
    if table.rows:
        parts.append(f"共 {len(table.rows)} 行")
    for column in table.columns:
        value = _column_numeric(table, column)
        if value is not None:
            unit = column.unit or ""
            parts.append(f"{column.name} {_decimal_str(value)}{unit}")
    unavailable = [column.name for column in table.columns if _column_unavailable(table, column)]
    if unavailable:
        parts.append(f"{'、'.join(unavailable)}：{_UNAVAILABLE_LABEL}")
    if not parts:
        return "未返回可用于生成摘要的数据。"
    return "，".join(parts) + "。"


def _column_unavailable(table: RenderTable, column: RenderColumn) -> bool:
    if not table.rows:
        return False
    value = table.rows[0].get(column.name)
    return value == UNAVAILABLE_VALUE


def _column_card_value(column: RenderColumn, table: RenderTable) -> object:
    if table.rows:
        first = table.rows[0].get(column.name)
        if first is not None:
            return _format_cell(first, column.column_type)
        return None
    return None


def _column_numeric(table: RenderTable, column: RenderColumn) -> Decimal | None:
    if column.name in table.totals:
        return table.totals[column.name]
    if table.rows:
        value = table.rows[0].get(column.name)
        if isinstance(value, Decimal):
            return value
        # Only typed numeric columns are treated as numbers; a string column
        # (e.g. a uid like "01001") must never be summarised as a figure.
        if column.column_type in ("money", "quantity", "percent") and isinstance(value, str):
            try:
                return Decimal(value)
            except Exception:  # noqa: BLE001 - non-numeric cells are skipped
                return None
    return None


def _format_cell(value: object, column_type: str | None) -> object:
    if value == UNAVAILABLE_VALUE:
        return _UNAVAILABLE_LABEL
    if isinstance(value, Decimal):
        return _decimal_str(value)
    if column_type == "money" and isinstance(value, str):
        return _decimal_str_value(value)
    if column_type == "date" and isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return value


def _decimal_str(value: Decimal) -> str:
    return format(value.normalize(), "f") if value == value.normalize() else format(value, "f")


def _decimal_str_value(value: str) -> str:
    try:
        return _decimal_str(Decimal(value))
    except Exception:  # noqa: BLE001 - non-numeric cells pass through
        return value


__all__ = ["build_card", "build_summary"]
