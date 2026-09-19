import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from factory_agent.api.server import create_app as create_factory_app


async def assert_service_liveness(app: FastAPI, service: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["service"] == service


@pytest.mark.asyncio
async def test_services_are_live() -> None:
    """One application process serves both the business and the statistics routes."""
    app = create_factory_app()
    await assert_service_liveness(app, "factory-agent")

    paths = app.openapi()["paths"]
    assert "/health/live" in paths
    assert any(path.startswith("/v1/statistics") for path in paths)
