from __future__ import annotations

from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.server import create_app

PATH = "/v1/piecework-records"
HEADERS = {
    "Authorization": "Bearer multi-tenant",
    "X-Tenant-Id": "tenant-a",
}
QUERY = {
    "authorized_employee_ids": "employee-a1",
    "authorized_dept_ids": "group-a1",
    "from": "2026-08-01T00:00:00Z",
    "to": "2026-09-01T00:00:00Z",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["duplicate_page", "missing_page", "wrong_total", "null", "field_drift"]
)
async def test_structural_fault_is_request_scoped(fault: str) -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        faulted = await client.get(PATH, headers={**HEADERS, "X-Mock-Fault": fault}, params=QUERY)
        normal = await client.get(PATH, headers=HEADERS, params=QUERY)

    assert faulted.status_code == 200
    payload = cast(dict[str, Any], faulted.json())
    items = cast(list[dict[str, Any]], payload["items"])
    if fault == "duplicate_page":
        assert len(items) == 2
    elif fault == "missing_page":
        assert items == [] and payload["total"] == 1
    elif fault == "wrong_total":
        assert payload["total"] == 8
    elif fault == "null":
        assert items[0]["status"] is None
    else:
        assert "synthetic_drift_field" in items[0]
    assert normal.json()["items"][0]["status"] == "unsettled"
    assert "synthetic_drift_field" not in normal.text


@pytest.mark.asyncio
async def test_status_and_latency_faults_are_opt_in() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        limited = await client.get(PATH, headers={**HEADERS, "X-Mock-Fault": "429"}, params=QUERY)
        failed = await client.get(PATH, headers={**HEADERS, "X-Mock-Fault": "5xx"}, params=QUERY)
        delayed = await client.get(
            PATH,
            headers={**HEADERS, "X-Mock-Fault": "latency", "X-Mock-Latency-Ms": "1"},
            params=QUERY,
        )
        health = await client.get("/health/live", headers={"X-Mock-Fault": "5xx"})

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "1"
    assert failed.status_code == 503
    assert delayed.status_code == 200
    assert health.status_code == 200
