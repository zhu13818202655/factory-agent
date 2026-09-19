"""Readiness must cover the statistics surface.

The standalone statistics ``/health`` endpoints are gone: there is one process
and one readiness endpoint, and it has to account for the statistics surface's
database as well as the business dependencies.
"""

import httpx
import pytest
from pydantic import PostgresDsn

from factory_agent.api.server import create_app
from factory_agent.config import FactoryAgentSettings


@pytest.mark.asyncio
async def test_readiness_reports_statistics_dependency() -> None:
    app = create_app(FactoryAgentSettings(environment="test"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "factory-agent"
    assert body["dependencies"]["statistics"] == "not_configured"


@pytest.mark.asyncio
async def test_readiness_reports_configured_statistics_without_disclosing_the_dsn() -> None:
    settings = FactoryAgentSettings(
        environment="test",
        postgres_url=PostgresDsn("postgresql://secret@example.invalid:5432/factory_agent"),
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["dependencies"]["statistics"] == "configured"
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_statistics_router_is_not_served_by_the_business_credential() -> None:
    """The two identity domains never authorize each other (D-2)."""
    app = create_app(FactoryAgentSettings(environment="test"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/statistics/tenants/registry",
            headers={
                "X-Factory-Credential": "any-credential",
                "X-Factory-Tenant-Id": "fac-01",
                "X-Factory-User-Id": "u-1",
            },
        )

    assert response.status_code == 403
