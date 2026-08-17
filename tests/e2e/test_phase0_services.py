from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mock_mes.api.server import create_app as create_mock_app

from factory_agent.api.server import create_app as create_factory_app


async def assert_service_liveness(app: FastAPI, service: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["service"] == service


@pytest.mark.asyncio
async def test_both_phase0_services_are_live() -> None:
    await assert_service_liveness(create_factory_app(), "factory-agent")
    await assert_service_liveness(create_mock_app(), "mock-mes")
