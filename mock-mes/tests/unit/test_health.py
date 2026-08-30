from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_liveness_reports_service_version(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mock-mes",
        "version": "0.1.0",
    }


@pytest.mark.asyncio
async def test_readiness_checks_the_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["service"] == "mock-mes"
