"""XLSX renderer tests (Story 6): valid workbook, formula neutralisation, totals."""

from __future__ import annotations

import zipfile
from decimal import Decimal
from io import BytesIO

from factory_agent.export.xlsx import render_xlsx
from factory_agent.ports.contracts import RenderColumn, RenderTable


def _table() -> RenderTable:
    return RenderTable(
        capability_id="fr003_personal_wage_detail",
        columns=(
            RenderColumn("rq", None, None, ("GongziMxQuery",), column_type="date"),
            RenderColumn("worktype", None, None, ("GongziMxQuery",)),
            RenderColumn("sl", None, None, ("GongziMxQuery",), column_type="quantity"),
            RenderColumn(
                "je",
                "payroll_amount",
                "customer-payroll-v1",
                ("GongziMxQuery",),
                column_type="money",
            ),
        ),
        rows=(
            {"rq": "2026-08-06", "worktype": "=SUM(A1)", "sl": Decimal("4"), "je": Decimal("4.00")},
        ),
        totals={"je": Decimal("4.00")},
        source_operations=("GongziMxQuery",),
        warnings=("口径未确认：Flag 默认扫描日期（C.12）",),
    )


def test_render_produces_a_valid_xlsx_workbook() -> None:
    content = render_xlsx(_table())
    assert content.startswith(b"PK")
    with zipfile.ZipFile(BytesIO(content)) as archive:
        assert "xl/workbook.xml" in archive.namelist()
        assert "xl/worksheets/sheet1.xml" in archive.namelist()


def test_formula_leading_text_is_neutralised() -> None:
    content = render_xlsx(_table())
    shared = _shared_strings(content)
    # The lead apostrophe forces Excel to treat it as text, never as a formula.
    assert "=SUM(A1)" in shared
    assert "':=SUM" not in shared


def test_totals_row_and_warnings_are_present() -> None:
    content = render_xlsx(_table())
    shared = _shared_strings(content)
    assert "合计" in shared
    assert "口径" in shared


def _shared_strings(content: bytes) -> str:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return ""
        return archive.read("xl/sharedStrings.xml").decode("utf-8")
