"""XLSX renderer consuming only a ``RenderTable``.

The renderer never re-queries the database or the MES; it writes the numbers,
types, units, and warning markers that already exist on the table. Monetary,
percentage, and date columns use dedicated number formats, the header row is
frozen, a totals row reflects the same numbers as the result card, and
cell values are neutralised against spreadsheet formula injection.
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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
    """Render the summary and complete detail tables without further data access."""
    if any(detail.detail_tables for detail in table.detail_tables):
        raise ValueError("export detail tables must have a single level")
    buffer = BytesIO()
    workbook: Any = xlsxwriter.Workbook(buffer, {"in_memory": True})
    formats: dict[str, Any] = {
        "header": workbook.add_format({"bold": True, "bg_color": "#DDEBF7"}),
        "money": workbook.add_format({"num_format": _MONEY_FORMAT}),
        "percent": workbook.add_format({"num_format": _PERCENT_FORMAT}),
        "date": workbook.add_format({"num_format": _DATE_FORMAT}),
        "quantity": workbook.add_format({"num_format": _QUANTITY_FORMAT}),
        "total": workbook.add_format({"bold": True, "bg_color": "#FCE4D6"}),
        "percent_total": workbook.add_format(
            {"bold": True, "bg_color": "#FCE4D6", "num_format": _PERCENT_FORMAT}
        ),
        "warning": workbook.add_format({"font_color": "#C00000"}),
    }
    used_titles: set[str] = set()
    main_title = _unique_sheet_title(sheet_title or table.title or "结果", used_titles)
    _write_table(workbook.add_worksheet(main_title), table, formats)
    for index, detail in enumerate(table.detail_tables, start=1):
        detail_title = _unique_sheet_title(detail.title or f"明细{index}", used_titles)
        _write_table(workbook.add_worksheet(detail_title), detail, formats)
    workbook.close()
    return buffer.getvalue()


def _write_table(worksheet: Any, table: RenderTable, formats: dict[str, Any]) -> None:
    """Write one complete result with its own totals and completeness metadata."""

    columns = tuple(column for column in table.columns if column.name not in table.hidden_columns)
    for index, column in enumerate(columns):
        # Worker-facing header: Chinese display title with the unit appended;
        # the stable identifier is only a fallback for unlabelled columns.
        header = column.title or column.name
        if column.unit:
            header = f"{header}（{column.unit}）"
        worksheet.write(0, index, header, formats["header"])
        if column.column_type == "money":
            worksheet.set_column(index, index, 14, formats["money"])
        elif column.column_type == "percent":
            worksheet.set_column(index, index, 12, formats["percent"])
        elif column.column_type == "date":
            worksheet.set_column(index, index, 12, formats["date"])
        elif column.column_type == "quantity":
            worksheet.set_column(index, index, 12, formats["quantity"])
        else:
            worksheet.set_column(index, index, 16)

    worksheet.freeze_panes(1, 0)

    for row_index, row in enumerate(table.rows, start=1):
        for column_index, column in enumerate(columns):
            value = _to_excel_value(row.get(column.name), column.column_type)
            cell_format = formats.get(column.column_type or "")
            if isinstance(value, (int, float, Decimal)):
                worksheet.write_number(row_index, column_index, float(value), cell_format)
            else:
                worksheet.write_string(row_index, column_index, safe_cell_text(value), cell_format)

    totals_row = len(table.rows) + 1
    if table.totals or table.incomplete:
        worksheet.write(totals_row, 0, "合计", formats["total"])
        for column_index, column in enumerate(columns):
            value = table.totals.get(column.name)
            if value is not None:
                numeric_value = value / Decimal("100") if column.column_type == "percent" else value
                total_format = formats[
                    "percent_total" if column.column_type == "percent" else "total"
                ]
                worksheet.write_number(totals_row, column_index, float(numeric_value), total_format)

    warning_row = totals_row + 1
    if table.warnings:
        worksheet.write(warning_row, 0, "口径/状态标注", formats["warning"])
        warning_row += 1
        for warning in table.warnings:
            worksheet.write(warning_row, 0, safe_cell_text(warning), formats["warning"])
            warning_row += 1
    if table.incomplete:
        worksheet.write(
            warning_row,
            0,
            safe_cell_text(f"状态：不完整（{table.incomplete_reason}）"),
            formats["warning"],
        )


def _to_excel_value(value: object, column_type: str | None) -> object:
    if value is None:
        return ""
    if value == UNAVAILABLE_VALUE:
        return _UNAVAILABLE_LABEL
    if column_type == "percent":
        try:
            return Decimal(str(value)) / Decimal("100")
        except (InvalidOperation, ValueError):
            return value
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


def _valid_sheet_title(title: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\\x00-\x1f]", "_", title).strip(" '")
    if cleaned.casefold() == "history":
        cleaned += "_"
    return cleaned[:31].rstrip("'") if cleaned else "Sheet"


def _unique_sheet_title(title: str, used_titles: set[str]) -> str:
    base = _valid_sheet_title(title)
    candidate = base
    sequence = 2
    while candidate.casefold() in used_titles:
        suffix = f" ({sequence})"
        candidate = f"{base[: 31 - len(suffix)]}{suffix}"
        sequence += 1
    used_titles.add(candidate.casefold())
    return candidate


__all__ = ["render_xlsx"]
