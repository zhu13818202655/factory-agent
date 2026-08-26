"""Wages golden fixture (Story 6): locks deterministic, hand-checkable numbers.

The golden locks, for the fixed employee ``01001`` over a cross-day/month window:

- the three-source detail rows (Type 0 扫码 / 1 吊挂 / 2 手工账) with the
  confirmed formula ``je = sl x price`` (M9/M18) holding on every row;
- the per-row total and the MES ``footer.je_total`` being identical;
- the summary (scheme=hz) grouped rows summing to the same gross total and
  piece count as the detail, so the two capability recipes stay consistent.

Any change to ``mock-mes/src/mock_mes/seed.py`` must update this golden file and
record the reason in the Story, otherwise the invariant test fails.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.customer import sign_of
from mock_mes.api.server import create_app

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
async def test_wages_golden_detail_locks_rows_and_footer() -> None:
    golden = _load_golden()
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    detail_rows = await _query(client, "")
    footer = await _query_footer(client)

    assert len(detail_rows) == golden["detail_rows"]
    expected = golden["detail"]
    for actual, exp in zip(detail_rows, expected, strict=True):
        assert actual["type"] in _PIECEWORK_SOURCES
        assert actual["rq"] == exp["rq"]
        assert actual["worktype"] == exp["worktype"]
        assert actual["sl"] == exp["sl"]
        assert actual["price"] == exp["price"]
        # Confirmed formula M9/M18: je = sl x price, exact Decimal.
        assert Decimal(actual["je"]) == Decimal(actual["sl"]) * Decimal(actual["price"])
        assert actual["je"] == exp["je"]

    # Per-row total must equal footer.je_total; a mismatch is a Mock defect.
    je_total = sum((Decimal(row["je"]) for row in detail_rows), Decimal("0"))
    assert je_total == Decimal(footer["je_total"])
    assert footer["sl_total"] == golden["footer"]["sl_total"]

    await client.aclose()


@pytest.mark.asyncio
async def test_wages_golden_summary_matches_detail() -> None:
    golden = _load_golden()
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

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

    await client.aclose()


async def _query_footer(client: AsyncClient) -> dict[str, str]:
    timestamp = int(datetime.now().timestamp())
    headers = {"Authorization": "Bearer MOCK-TOKEN-01001"}
    body = {
        "app_key": "APPKEY-A",
        "timestamp": timestamp,
        "sign": sign_of("APPKEY-A", timestamp),
        "Uid": "01001",
        "Flag": "0",
        "Type": "0,1,2",
        "scheme": "",
        "queryFooter": "1",
        "dates": "2026-07-01",
        "datee": "2026-08-31",
        "page": 1,
        "size": 200,
    }
    response = (
        await client.post("/api/NetYf/Sclzd/GongziMxQuery", json=body, headers=headers)
    ).json()
    result = cast("dict[str, Any]", response["result"])
    footer = cast("dict[str, str]", result["footer"])
    return footer
