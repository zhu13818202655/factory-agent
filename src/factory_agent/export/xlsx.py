"""XLSX renderer consuming only a ``RenderTable`` (Story 6).

The renderer never re-queries the database or the MES; it writes the numbers,
types, units, and warning markers that already exist on the table. Monetary,
percentage, and date columns use dedicated number formats, the header row is
frozen, a totals row reflects the same numbers as the result card, and
cell values are neutralised against spreadsheet formula injection.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any

import xlsxwriter  # pyright: ignore[reportMissingTypeStubs]

from factory_agent.export.sanitize import safe_cell_text
from factory_agent.ports.contracts import UNAVAILABLE_VALUE, RenderTable

_MONEY_FORMAT = "#,##0.00"
_PERCENT_FORMAT = "0.00%"
_DATE_FORMAT = "yyyy-mm-dd"
_QUANTITY_FORMAT = "#,##0"
_UNAVAILABLE_LABEL = "暂无数据源"


def render_xlsx(table: RenderTable, *, sheet_title: str | None = None) -> bytes:
    """Render a table to an XLSX workbook and return its bytes."""
    buffer = BytesIO()
    workbook: Any = xlsxwriter.Workbook(buffer, {"in_memory": True})
    worksheet: Any = workbook.add_worksheet(_valid_sheet_title(sheet_title or "结果"))

    header_format: Any = workbook.add_format({"bold": True, "bg_color": "#DDEBF7"})
    money_format: Any = workbook.add_format({"num_format": _MONEY_FORMAT})
    percent_format: Any = workbook.add_format({"num_format": _PERCENT_FORMAT})
    date_format: Any = workbook.add_format({"num_format": _DATE_FORMAT})
    quantity_format: Any = workbook.add_format({"num_format": _QUANTITY_FORMAT})
    total_format: Any = workbook.add_format({"bold": True, "bg_color": "#FCE4D6"})
    warning_format: Any = workbook.add_format({"font_color": "#C00000"})

    columns = table.columns
    for index, column in enumerate(columns):
        header = column.unit if column.unit else column.name
        worksheet.write(0, index, header, header_format)
        if column.column_type == "money":
            worksheet.set_column(index, index, 14, money_format)
        elif column.column_type == "percent":
            worksheet.set_column(index, index, 12, percent_format)
        elif column.column_type == "date":
            worksheet.set_column(index, index, 12, date_format)
        elif column.column_type == "quantity":
            worksheet.set_column(index, index, 12, quantity_format)
        else:
            worksheet.set_column(index, index, 16)

    worksheet.freeze_panes(1, 0)

    for row_index, row in enumerate(table.rows, start=1):
        for column_index, column in enumerate(columns):
            value = _to_excel_value(row.get(column.name), column.column_type)
            cell_format = _format_for(
                column.column_type, money_format, percent_format, date_format, quantity_format
            )
            if isinstance(value, (int, float, Decimal)):
                worksheet.write_number(row_index, column_index, float(value), cell_format)
            else:
                worksheet.write_string(row_index, column_index, safe_cell_text(value), cell_format)

    totals_row = len(table.rows) + 1
    if table.totals or table.incomplete:
        worksheet.write(totals_row, 0, "合计", total_format)
        for column_index, column in enumerate(columns):
            value = table.totals.get(column.name)
            if value is not None:
                worksheet.write_number(totals_row, column_index, float(value), total_format)

    warning_row = totals_row + 1
    if table.warnings:
        worksheet.write(warning_row, 0, "口径/状态标注", warning_format)
        warning_row += 1
        for warning in table.warnings:
            worksheet.write(warning_row, 0, safe_cell_text(warning), warning_format)
            warning_row += 1
    if table.incomplete:
        worksheet.write(
            warning_row,
            0,
            safe_cell_text(f"状态：不完整（{table.incomplete_reason}）"),
            warning_format,
        )

    workbook.close()
    return buffer.getvalue()


def _to_excel_value(value: object, column_type: str | None) -> object:
    if value is None:
        return ""
    if value == UNAVAILABLE_VALUE:
        return _UNAVAILABLE_LABEL
    if isinstance(value, Decimal):
        return value
    if column_type == "date" and isinstance(value, str) and len(value) >= 10:
        return value[:10]
    if column_type in ("money", "quantity") and isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:  # noqa: BLE001 - non-numeric cells stay text
            return value
    if isinstance(value, (datetime, date)):
        return value
    return value


def _format_for(
    column_type: str | None,
    money_format: Any,
    percent_format: Any,
    date_format: Any,
    quantity_format: Any,
) -> Any:
    if column_type == "money":
        return money_format
    if column_type == "percent":
        return percent_format
    if column_type == "date":
        return date_format
    if column_type == "quantity":
        return quantity_format
    return None


def _valid_sheet_title(title: str) -> str:
    import re

    cleaned = re.sub(r"[\[\]:*?/\\]", "_", title)
    return cleaned[:31] if cleaned else "Sheet"


__all__ = ["render_xlsx"]
