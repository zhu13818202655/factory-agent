from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from usage_admin.api.server import create_app
from usage_admin.config import UsageAdminSettings


@pytest.mark.asyncio
async def test_liveness_reports_service_version() -> None:
    transport = ASGITransport(app=create_app(UsageAdminSettings()))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "usage-admin",
        "version": "0.1.0",
    }


@pytest.mark.asyncio
async def test_readiness_reports_missing_database_without_blocking_health() -> None:
    transport = ASGITransport(app=create_app(UsageAdminSettings(database_url=None)))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "service": "usage-admin",
        "version": "0.1.0",
        "database": "not_configured",
    }


@pytest.mark.asyncio
async def test_readiness_reports_database_configuration_without_disclosing_url() -> None:
    settings = UsageAdminSettings(database_url=SecretStr("postgresql://secret@example/usage"))
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["database"] == "configured"
    assert "secret" not in response.text
