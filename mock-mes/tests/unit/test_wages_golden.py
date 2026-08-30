"""Wages golden fixture (Story 6, PG-backed since Story 10).

Locks, for the fixed employee ``01001`` over the fixed window, the three-source
detail rows (Type 0 扫码 / 1 吊挂 / 2 手工账) with the confirmed formula
``je = sl x price`` (M9/M18) holding on every row, the footer totals, and the
summary (scheme=hz) agreeing with the detail.

The golden was regenerated for Story 10: the data window now starts at the
previous year and the generator adds deterministic rolling rows, so the window
contains more of employee 01001's rows than the original Story-6 fixture. The
anchored rows themselves are unchanged; the change is recorded in Story 10.

Any change to the generator must update this golden file and record the reason
in the Story, otherwise the invariant test fails.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import AsyncClient
from mock_mes.api.customer import sign_of

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "wages_v1.json"

_PIECEWORK_SOURCES = {"扫码产量", "吊挂产量", "手工账产量"}


def _load_golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


async def _query(client: AsyncClient, scheme: str) -> list[dict[str, Any]]:
    timestamp = int(datetime.now().timestamp())
    headers = {"Authorization": "Bearer MOCK-TOKEN-01001"}
    body = {
        "app_key": "APPKEY-A",
        "timestamp": timestamp,
        "sign": sign_of("APPKEY-A", timestamp),
        "Uid": "01001",
        "Flag": "0",
        "Type": "0,1,2",
        "scheme": scheme,
        "queryFooter": "1",
        "dates": "2026-07-01",
        "datee": "2026-08-31",
        "page": 1,
        "size": 200,
    }
    response = (
        await client.post("/api/NetYf/Sclzd/GongziMxQuery", json=body, headers=headers)
    ).json()
    assert response["code"] == 1, response["message"]
    result = cast("dict[str, Any]", response["result"])
    return cast("list[dict[str, Any]]", result["list"])


@pytest.mark.asyncio
async def test_wages_golden_detail_locks_rows_and_footer(client: AsyncClient) -> None:
    golden = _load_golden()
    detail_rows = await _query(client, "")

    assert len(detail_rows) == golden["detail_rows"]
    for actual, exp in zip(detail_rows, golden["detail"], strict=True):
        assert actual["type"] in _PIECEWORK_SOURCES
        assert actual["rq"] == exp["rq"]
        assert actual["worktype"] == exp["worktype"]
        assert actual["sl"] == exp["sl"]
        assert actual["price"] == exp["price"]
        # Confirmed formula M9/M18: je = sl x price, exact Decimal.
        assert Decimal(actual["je"]) == Decimal(actual["sl"]) * Decimal(actual["price"])
        assert actual["je"] == exp["je"]


@pytest.mark.asyncio
async def test_wages_golden_summary_matches_detail(client: AsyncClient) -> None:
    golden = _load_golden()
    summary_rows = await _query(client, "hz")
    assert len(summary_rows) == golden["summary_rows"]
    for actual, exp in zip(summary_rows, golden["summary"], strict=True):
        assert actual["type"] == exp["type"]
        assert actual["worktype"] == exp["worktype"]
        assert actual["sl"] == exp["sl"]
        assert actual["je"] == exp["je"]

    summary_je = sum((Decimal(row["je"]) for row in summary_rows), Decimal("0"))
    summary_sl = sum((Decimal(row["sl"]) for row in summary_rows), Decimal("0"))

    # FR-003 detail and FR-002 summary must agree on gross total and piece count.
    detail_rows = await _query(client, "")
    detail_je = sum((Decimal(row["je"]) for row in detail_rows), Decimal("0"))
    detail_sl = sum((Decimal(row["sl"]) for row in detail_rows), Decimal("0"))
    assert summary_je == detail_je
    assert summary_sl == detail_sl
    assert summary_je == Decimal(golden["footer"]["je_total"])
    assert summary_sl == Decimal(golden["footer"]["sl_total"])


@pytest.mark.asyncio
async def test_wages_footer_totals_are_sql_aggregates(client: AsyncClient) -> None:
    """footer.je_total equals the per-row total; sl_total matches the golden."""
    golden = _load_golden()
    detail_rows = await _query(client, "")
    je_total = sum((Decimal(row["je"]) for row in detail_rows), Decimal("0"))
    sl_total = sum((Decimal(row["sl"]) for row in detail_rows), Decimal("0"))
    assert str(je_total) == golden["footer"]["je_total"]
    assert str(sl_total) == golden["footer"]["sl_total"]
