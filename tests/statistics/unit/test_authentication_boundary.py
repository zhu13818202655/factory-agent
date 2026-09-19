"""The two identity domains must not cross.

The business surface authenticates a factory MES credential; the statistics
surface authenticates a platform Bearer token. Neither credential is accepted by
the other, and no handler infers identity from "whichever credential showed up"
(D-2). These tests are the executable form of that rule.
"""

from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from factory_agent.api.server import create_app
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import Role
from factory_agent.statistics.api.router import statistics_router
from factory_agent.statistics.config import StatisticsSettings
from factory_agent.statistics.container import build_container
from factory_agent.statistics.store import InMemoryUsageStore
from tests.support.session import FrozenClock
from tests.support.token import FakeCredentialExchange, principal

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
CREDENTIAL_HEADER = "X-Factory-Credential"
CREDENTIAL = "enc-tenant-a-user-a"
API_TOKEN = "platform-api-token"
BUSINESS_ROUTE = "/v1/quick-questions"
STATISTICS_ROUTE = "/v1/statistics/tenants/registry"


def business_app() -> FastAPI:
    exchange = FakeCredentialExchange(
        {CREDENTIAL: principal(tenant_id="tenant-a", user_id="user-a", role=Role.EMPLOYEE)}
    )
    settings = FactoryAgentSettings(environment="test")
    return create_app(
        settings,
        DependencyOverrides(clock=FrozenClock(NOW), credential_exchange=exchange),
    )


def statistics_app() -> FastAPI:
    app = FastAPI(title="statistics-access-test")
    app.state.statistics = build_container(
        StatisticsSettings(api_token=SecretStr(API_TOKEN)),
        store=InMemoryUsageStore(),
    )
    app.include_router(statistics_router)
    return app


def http(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


@pytest.mark.asyncio
async def test_business_route_rejects_a_platform_bearer_token() -> None:
    async with http(business_app()) as client:
        response = await client.get(
            BUSINESS_ROUTE, headers={"Authorization": f"Bearer {API_TOKEN}"}
        )

    assert response.status_code == 401
    assert "credential" in response.text


@pytest.mark.asyncio
async def test_statistics_route_rejects_a_factory_credential() -> None:
    async with http(statistics_app()) as client:
        response = await client.get(STATISTICS_ROUTE, headers={CREDENTIAL_HEADER: CREDENTIAL})

    assert response.status_code == 403
    assert "bearer" in response.text


@pytest.mark.asyncio
async def test_statistics_route_rejects_an_arbitrary_authorization_header() -> None:
    """A header that merely asserts an identity is not an identity."""
    async with http(statistics_app()) as client:
        response = await client.get(
            STATISTICS_ROUTE, headers={"Authorization": f"Bearer {CREDENTIAL}"}
        )

    assert response.status_code == 403
    assert "invalid or expired" in response.text


@pytest.mark.asyncio
async def test_business_route_ignores_a_platform_bearer_token_alongside_bad_credentials() -> None:
    """A valid platform token never rescues a request missing its factory credential."""
    async with http(business_app()) as client:
        response = await client.get(
            BUSINESS_ROUTE,
            headers={"Authorization": f"Bearer {API_TOKEN}", CREDENTIAL_HEADER: "not-a-credential"},
        )

    assert response.status_code == 401
    assert "rejected" in response.text
