"""Fault injection tests adapted to the customer envelope (PG-backed, Story 10)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from httpx import AsyncClient

HEADERS = {"Authorization": "Bearer MOCK-TOKEN-01009"}
WINDOW = {"dates": "2026-07-01", "datee": "2026-08-31"}


async def login(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/system/token", json={"app_key": "APPKEY-A"})
    return cast(dict[str, Any], response.json()["result"])


def common(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "app_key": result["appkey"],
        "timestamp": result["timestamp"],
        "sign": result["sign"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["duplicate_page", "missing_page", "wrong_total", "footer_mismatch", "null", "field_drift"],
)
async def test_structural_fault_is_request_scoped(client: AsyncClient, fault: str) -> None:
    result = await login(client)
    params = {
        **common(result),
        "page": 1,
        "size": 50,
        "Uid": "01001",
        **WINDOW,
    }
    faulted = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json=params,
        headers={**HEADERS, "X-Mock-Fault": fault},
    )
    normal = await client.post("/api/NetYf/Sclzd/YskQuery", json=params, headers=HEADERS)

    assert faulted.status_code == 200
    payload = faulted.json()["result"]
    if fault == "duplicate_page":
        # One row more than the page size: pagination no longer matches total.
        assert len(payload["list"]) == int(params["size"]) + 1
    elif fault == "missing_page":
        assert payload["list"] == [] and payload["total"] > 0
    elif fault == "wrong_total":
        assert payload["total"] != len(payload["list"])
    elif fault == "footer_mismatch":
        assert payload["footer"]["sl_total"] is not None
    elif fault == "null":
        assert any(value is None for value in payload["list"][0].values())
    else:
        assert "synthetic_drift_field" in payload["list"][0]
    assert fault not in normal.text


@pytest.mark.asyncio
async def test_status_and_latency_faults_are_opt_in(client: AsyncClient) -> None:
    result = await login(client)
    params = {**common(result), "page": 1, "size": 50, "Uid": "01001", **WINDOW}
    limited = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json=params,
        headers={**HEADERS, "X-Mock-Fault": "429"},
    )
    failed = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json=params,
        headers={**HEADERS, "X-Mock-Fault": "5xx"},
    )
    missing_endpoint = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json=params,
        headers={**HEADERS, "X-Mock-Fault": "404"},
    )
    delayed = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json=params,
        headers={**HEADERS, "X-Mock-Fault": "latency", "X-Mock-Latency-Ms": "1"},
    )
    health = await client.get("/health/live", headers={"X-Mock-Fault": "5xx"})

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "1"
    assert failed.status_code == 503
    assert missing_endpoint.status_code == 404
    assert delayed.status_code == 200
    assert health.status_code == 200
