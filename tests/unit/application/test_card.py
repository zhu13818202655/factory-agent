"""Structured card builder tests (Story 6): numbers only, no fabrication."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from factory_agent.application.card import build_card, build_summary
from factory_agent.ports.contracts import RenderColumn, RenderTable


def _table() -> RenderTable:
    return RenderTable(
        capability_id="fr002_personal_wage_summary",
        columns=(
            RenderColumn(
                "gross_total", None, None, ("GongziMxQuery",), column_type="money", unit="元"
            ),
            RenderColumn(
                "piece_count", None, None, ("GongziMxQuery",), column_type="quantity", unit="件"
            ),
            RenderColumn(
                "daily_avg", None, None, ("GongziMxQuery",), column_type="money", unit="元"
            ),
        ),
        rows=(
            {
                "gross_total": Decimal("21.65"),
                "piece_count": Decimal("20"),
                "daily_avg": Decimal("0.35"),
            },
        ),
        totals={"gross_total": Decimal("21.65"), "piece_count": Decimal("20")},
        source_operations=("GongziMxQuery",),
        warnings=("日均分母为我方定义（自然日），非客户口径",),
    )


def test_card_only_references_existing_numbers() -> None:
    card: dict[str, Any] = cast("dict[str, Any]", build_card(_table()))
    totals: dict[str, Any] = cast("dict[str, Any]", card["totals"])
    columns: list[dict[str, Any]] = cast("list[dict[str, Any]]", card["columns"])
    warnings: list[str] = cast("list[str]", card["warnings"])

    assert card["kind"] == "result_card"
    assert totals["gross_total"] == "21.65"
    assert totals["piece_count"] == "20"
    assert card["row_count"] == 1
    assert warnings[0].startswith("日均分母")
    assert columns[0]["unit"] == "元"
    assert columns[0]["type"] == "money"


def test_card_marks_incomplete_results() -> None:
    table = _table()
    table = RenderTable(
        capability_id=table.capability_id,
        columns=table.columns,
        rows=table.rows,
        totals=table.totals,
        source_operations=table.source_operations,
        warnings=table.warnings,
        incomplete=True,
        incomplete_reason="pagination_total_drift",
    )
    card = build_card(table)

    assert card["incomplete"] is True
    assert card["incomplete_reason"] == "pagination_total_drift"


def test_summary_never_fabricates_numbers() -> None:
    summary = build_summary(_table())

    assert "21.65" in summary
    assert "20" in summary
    assert "0.35" in summary


def test_summary_of_empty_table_is_explicit() -> None:
    empty = RenderTable(
        capability_id="fr002_personal_wage_summary",
        columns=_table().columns,
        rows=(),
        totals={},
        source_operations=("GongziMxQuery",),
    )
    assert build_summary(empty) == "未返回可用于生成摘要的数据。"
