from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from usage_admin.api.server import create_app as create_usage_admin_app

from factory_agent.api.server import create_app as create_factory_app


async def assert_service_liveness(app: FastAPI, service: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["service"] == service


@pytest.mark.asyncio
async def test_story_one_services_are_live(mock_mes_app: Any) -> None:
    await assert_service_liveness(create_factory_app(), "factory-agent")
    # The mock-mes app is PG-backed; liveness works the same way.
    await assert_service_liveness(mock_mes_app, "mock-mes")
    await assert_service_liveness(create_usage_admin_app(), "usage-admin")
