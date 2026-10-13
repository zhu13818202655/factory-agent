"""XLSX renderer tests: valid workbook, formula neutralisation, totals."""

import socket
import zipfile
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from typing import NoReturn
from xml.etree import ElementTree

import pytest

from factory_agent.export.xlsx import render_xlsx
from factory_agent.ports.contracts import RenderColumn, RenderTable

_SHEET_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _table() -> RenderTable:
    return RenderTable(
        capability_id="fr003_personal_wage_detail",
        columns=(
            RenderColumn("rq", None, None, ("GongziMxQuery",), column_type="date", title="日期"),
            RenderColumn("worktype", None, None, ("GongziMxQuery",), title="工序"),
            RenderColumn(
                "sl",
                None,
                None,
                ("GongziMxQuery",),
                column_type="quantity",
                unit="件",
                title="完成数量",
            ),
            RenderColumn(
                "je",
                "payroll_amount",
                "customer-payroll-v1",
                ("GongziMxQuery",),
                column_type="money",
                unit="元",
                title="小计金额",
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


def test_headers_use_chinese_titles_with_units() -> None:
    """Workers read Chinese headers: title first, unit appended in parentheses."""
    shared = _shared_strings(render_xlsx(_table()))
    assert "日期" in shared
    assert "工序" in shared
    assert "完成数量（件）" in shared
    assert "小计金额（元）" in shared
    # Raw identifiers never appear as header text when a title exists.
    assert ">rq<" not in shared
    assert ">sl<" not in shared


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("100"), Decimal("1")),
        ("33.33", Decimal("0.3333")),
        (100, Decimal("1")),
        (12.5, Decimal("0.125")),
        ("0", Decimal("0")),
    ],
)
def test_percent_values_use_fractional_excel_numbers(value: object, expected: Decimal) -> None:
    table = replace(
        _table(),
        columns=(RenderColumn("ratio", None, None, (), column_type="percent", unit="%"),),
        rows=({"ratio": value},),
        totals={},
    )
    content = render_xlsx(table)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cell = sheet.find("s:sheetData/s:row/s:c[@r='A2']", _SHEET_NS)
        assert cell is not None
        assert Decimal(cell.findtext("s:v", namespaces=_SHEET_NS) or "") == expected
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        formats = styles.findall("s:cellXfs/s:xf", _SHEET_NS)
        format_id = formats[int(cell.attrib["s"])].attrib["numFmtId"]
        assert _number_format_code(styles, format_id) == "0.00%"


def test_percent_totals_use_the_same_percentage_scale_and_format() -> None:
    table = replace(
        _table(),
        columns=(
            RenderColumn("name", None, None, ()),
            RenderColumn("ratio", None, None, (), column_type="percent"),
        ),
        rows=({"name": "计划一", "ratio": Decimal("33.33")},),
        totals={"ratio": Decimal("33.33")},
    )
    with zipfile.ZipFile(BytesIO(render_xlsx(table))) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cell = sheet.find("s:sheetData/s:row/s:c[@r='B3']", _SHEET_NS)
        assert cell is not None
        assert cell.findtext("s:v", namespaces=_SHEET_NS) == "0.3333"
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        formats = styles.findall("s:cellXfs/s:xf", _SHEET_NS)
        format_id = formats[int(cell.attrib["s"])].attrib["numFmtId"]
        assert _number_format_code(styles, format_id) == "0.00%"


def test_all_detail_rows_export_beyond_the_card_preview_limit() -> None:
    detail = replace(
        _table(),
        title="制单明细",
        rows=tuple(
            {"rq": "2026-09-01", "worktype": f"工序{index}", "sl": Decimal(index)}
            for index in range(1, 126)
        ),
        totals={"sl": Decimal("7875")},
        warnings=("缓存生成时间：2026-10-12T15:02:38+00:00",),
        incomplete=True,
        incomplete_reason="scope_narrowed",
    )
    table = replace(_table(), title="订单汇总", detail_tables=(detail,))
    content = render_xlsx(table)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        names = [sheet.attrib["name"] for sheet in workbook.findall("s:sheets/s:sheet", _SHEET_NS)]
        assert names == ["订单汇总", "制单明细"]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        cells = sheet.findall("s:sheetData/s:row/s:c", _SHEET_NS)
        by_reference = {cell.attrib["r"]: cell for cell in cells}
        assert by_reference["C126"].findtext("s:v", namespaces=_SHEET_NS) == "125"
        assert by_reference["C127"].findtext("s:v", namespaces=_SHEET_NS) == "7875"
    shared = _shared_strings(content)
    assert "工序125" in shared
    assert "缓存生成时间：2026-10-12T15:02:38+00:00" in shared
    assert "状态：不完整（scope_narrowed）" in shared


def test_multiple_sheets_have_legal_case_insensitive_unique_names() -> None:
    long_title = "制单" * 20
    titles = ("总览/结果", "Orders", "orders", long_title, long_title, "'", "History")
    table = replace(
        _table(),
        title=titles[0],
        detail_tables=tuple(replace(_table(), title=title) for title in titles),
    )
    with zipfile.ZipFile(BytesIO(render_xlsx(table))) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        names = [sheet.attrib["name"] for sheet in workbook.findall("s:sheets/s:sheet", _SHEET_NS)]
    assert len(names) == len(titles) + 1
    assert len({name.casefold() for name in names}) == len(names)
    assert all(1 <= len(name) <= 31 for name in names)
    assert all(not any(char in name for char in "[]:*?/\\") for name in names)
    assert all(not name.startswith("'") and not name.endswith("'") for name in names)
    assert names[:4] == ["总览_结果", "总览_结果 (2)", "Orders", "orders (2)"]


def test_unavailable_percent_is_not_exported_as_zero() -> None:
    table = replace(
        _table(),
        columns=(RenderColumn("ratio", None, None, (), column_type="percent"),),
        rows=({"ratio": "unavailable"}, {"ratio": None}),
        totals={},
    )
    content = render_xlsx(table)
    assert "暂无数据源" in _shared_strings(content)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cell = sheet.find("s:sheetData/s:row/s:c[@r='A2']", _SHEET_NS)
        assert cell is not None
        assert cell.attrib["t"] == "s"


def test_nested_details_are_rejected_instead_of_silently_omitted() -> None:
    nested = replace(_table(), detail_tables=(_table(),))
    table = replace(_table(), detail_tables=(nested,))
    with pytest.raises(ValueError, match="single level"):
        render_xlsx(table)


def test_summary_and_details_export_without_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_socket(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("the export renderer must not fetch business data")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    content = render_xlsx(replace(_table(), detail_tables=(_table(), _table())))
    with zipfile.ZipFile(BytesIO(content)) as archive:
        assert "xl/worksheets/sheet3.xml" in archive.namelist()
        assert "小计金额（元）" in _shared_strings(content)


def _number_format_code(styles: ElementTree.Element, format_id: str) -> str:
    if format_id == "10":
        return "0.00%"
    entry = styles.find(f"s:numFmts/s:numFmt[@numFmtId='{format_id}']", _SHEET_NS)
    assert entry is not None
    return entry.attrib["formatCode"]


def _shared_strings(content: bytes) -> str:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return ""
        return archive.read("xl/sharedStrings.xml").decode("utf-8")
