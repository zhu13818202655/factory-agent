"""Personalization API endpoints over an in-memory container."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from factory_agent.api.personal import personal_router
from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.personal import PersonalizationService
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from tests.support.personal import (
    InMemoryFavoriteRepository,
    InMemoryHistoryRepository,
    InMemoryUserMappingRepository,
)
from tests.support.session import FrozenClock

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"}


def make_client() -> httpx.AsyncClient:
    personalization = PersonalizationService(
        InMemoryHistoryRepository(),
        InMemoryFavoriteRepository(),
        InMemoryUserMappingRepository(),
        new_id=lambda: "id-1",
        clock=lambda: NOW,
    )
    overrides = DependencyOverrides(
        clock=FrozenClock(NOW),
        personalization=personalization,
    )
    app = create_app(FactoryAgentSettings(environment="test"), overrides)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


@pytest.mark.asyncio
async def test_quick_questions_require_identity_and_return_registered_capabilities() -> None:
    async with make_client() as http:
        response = await http.get("/v1/quick-questions")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_quick_questions_return_registered_capabilities() -> None:
    async with make_client() as http:
        response = await http.get("/v1/quick-questions", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert 4 <= len(body) <= 6
    assert {item["capability_id"] for item in body} <= {
        "FR-001",
        "FR-002",
        "FR-007",
        "FR-009",
        "FR-011",
    }


@pytest.mark.asyncio
async def test_history_list_is_ownership_filtered() -> None:
    async with make_client() as http:
        await http.post(
            "/v1/users/me/mapping", json={"uname": "张三", "company": "工厂A"}, headers=HEADERS
        )
        response = await http.get("/v1/history", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_favorite_create_list_delete_roundtrip() -> None:
    async with make_client() as http:
        created = await http.post(
            "/v1/favorites",
            json={
                "capability_id": "FR-001",
                "title": "个人产量",
                "slots": {"time_expression": "本月", "employee_ids": ["E-1"]},
            },
            headers=HEADERS,
        )
        assert created.status_code == 201
        favorite = created.json()
        # Sensitive slots are stripped by the service.
        assert favorite["slots"] == {"time_expression": "本月"}

        listed = await http.get("/v1/favorites", headers=HEADERS)
        assert listed.status_code == 200
        assert [item["favorite_id"] for item in listed.json()] == [favorite["favorite_id"]]

        reasked = await http.post(
            f"/v1/favorites/{favorite['favorite_id']}/re-ask", headers=HEADERS
        )
        assert reasked.status_code == 200
        assert reasked.json()["capability_id"] == "FR-001"

        deleted = await http.delete(f"/v1/favorites/{favorite['favorite_id']}", headers=HEADERS)
        assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_cross_user_favorite_is_not_visible() -> None:
    async with make_client() as http:
        created = await http.post(
            "/v1/favorites",
            json={"capability_id": "FR-001", "title": "t", "slots": {}},
            headers=HEADERS,
        )
        favorite_id = created.json()["favorite_id"]

        other = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-b"}
        reasked = await http.post(f"/v1/favorites/{favorite_id}/re-ask", headers=other)

    assert reasked.status_code == 404


@pytest.mark.asyncio
async def test_user_mapping_roundtrip() -> None:
    async with make_client() as http:
        saved = await http.post(
            "/v1/users/me/mapping", json={"uname": "李四", "company": None}, headers=HEADERS
        )

    assert saved.status_code == 200
    assert saved.json() == {"uid": "user-a", "uname": "李四", "company": None}


@pytest.mark.asyncio
async def test_personal_router_is_registered() -> None:
    assert personal_router.prefix == "/v1"
    paths = {str(getattr(route, "path", "")) for route in personal_router.routes}
    assert "/v1/quick-questions" in paths
    assert "/v1/history" in paths
    assert "/v1/favorites" in paths
