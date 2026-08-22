from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from factory_agent.api.server import create_app


@pytest.mark.asyncio
async def test_liveness_reports_service_version() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "factory-agent",
        "version": "0.1.0",
    }


@pytest.mark.asyncio
async def test_readiness_reports_service_version() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["service"] == "factory-agent"
    assert response.json()["status"] == "degraded"
    assert response.json()["dependencies"]["mes"] == "not_configured"
