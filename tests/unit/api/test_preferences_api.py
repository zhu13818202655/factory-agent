"""Push preferences + morning report API tests (Story 3B)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.preferences import PreferencesService
from factory_agent.application.reporting import DirectReportRunner, ReportingService
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import Role
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.push import InMemoryPushPreferenceRepository
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
)

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"}


def _overrides(
    *,
    role: Role = Role.EMPLOYEE,
) -> DependencyOverrides:
    member = membership("user-a", "tenant-a", "emp-1", role)
    authorization = AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={("tenant-a", "user-a"): member}
        ),
        organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
        versions=FixedScopeVersionAssigner(),
    )
    preferences = PreferencesService(InMemoryPushPreferenceRepository())
    store = InMemoryInteractionStore()
    runner = RecordingCapabilityRunner()
    reporting = ReportingService(
        DirectReportRunner(authorization, runner, clock=FrozenClock(NOW)),
        None,
        clock=FrozenClock(NOW),
    )
    return DependencyOverrides(
        model=ScriptedModelGateway(contents=[]),
        clock=FrozenClock(NOW),
        authorization=authorization,
        interactions=store,
        capability_runner=runner,
        preferences_service=preferences,
        reporting=reporting,
    )


def _client(role: Role = Role.EMPLOYEE) -> httpx.AsyncClient:
    app = create_app(FactoryAgentSettings(environment="test"), _overrides(role=role))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


@pytest.mark.asyncio
async def test_preferences_defaults_and_options_are_role_filtered() -> None:
    async with _client() as http:
        defaults = await http.get("/v1/push/preferences", headers=HEADERS)
        options = await http.get("/v1/push/preferences/options", headers=HEADERS)

    assert defaults.status_code == 200
    body = defaults.json()
    assert body["morning_report_enabled"] is True  # 早报固定默认，不可关闭
    assert body["content_items"] == []
    item_ids = [item["item_id"] for item in options.json()["items"]]
    assert item_ids == ["wage_detail_push"]  # 员工仅本人工资推送可订阅


@pytest.mark.asyncio
async def test_update_preferences_persists_and_owner_gets_management_options() -> None:
    async with _client(role=Role.OWNER) as http:
        updated = await http.put(
            "/v1/push/preferences",
            headers=HEADERS,
            json={
                "weekly_enabled": True,
                "weekly_day_of_week": 1,
                "weekly_time": "09:00",
                "content_items": ["completion_overview", "wage_detail_push"],
            },
        )
        options = await http.get("/v1/push/preferences/options", headers=HEADERS)

    assert updated.status_code == 200
    assert updated.json()["weekly_enabled"] is True
    owner_items = {item["item_id"] for item in options.json()["items"]}
    assert "completion_overview" in owner_items


@pytest.mark.asyncio
async def test_update_rejects_out_of_role_content_items() -> None:
    async with _client(role=Role.EMPLOYEE) as http:
        response = await http.put(
            "/v1/push/preferences",
            headers=HEADERS,
            json={"content_items": ["completion_overview"]},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_morning_report_generates_yesterday_summary_for_caller() -> None:
    async with _client() as http:
        response = await http.get("/v1/push/morning-report", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "employee"
    assert body["date_label"] == "2026-09-02"
    assert len(body["sections"]) == 2
    assert body["delivered"] is True
    assert body["body"].startswith("【")
