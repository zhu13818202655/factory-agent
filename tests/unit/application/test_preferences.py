"""Push preference service tests (Story 3B)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.preferences import (
    PreferencesService,
    PreferenceValidationError,
)
from factory_agent.domain import Role, TenantId, UserId
from tests.support.push import InMemoryPushPreferenceRepository
from tests.support.session import FrozenClock

NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
TENANT = TenantId("APPKEY-A")
USER = UserId("01001")


@pytest.mark.asyncio
async def test_get_returns_defaults_when_nothing_stored() -> None:
    service = PreferencesService(InMemoryPushPreferenceRepository(), clock=FrozenClock(NOW))

    prefs = await service.get(TENANT, USER)

    assert prefs.content_items == ()
    assert prefs.weekly_enabled is False
    assert prefs.monthly_enabled is False


@pytest.mark.asyncio
async def test_update_persists_weekly_cadence_and_role_filtered_items() -> None:
    service = PreferencesService(InMemoryPushPreferenceRepository(), clock=FrozenClock(NOW))

    updated = await service.update(
        TENANT,
        USER,
        Role.EMPLOYEE,
        weekly_enabled=True,
        weekly_day_of_week=1,
        weekly_time="09:00",
        content_items=("wage_detail_push",),
    )

    assert updated.weekly_enabled is True
    assert updated.weekly_day_of_week == 1
    stored = await service.get(TENANT, USER)
    assert stored.weekly_time == "09:00"
    assert stored.content_items == ("wage_detail_push",)


@pytest.mark.asyncio
async def test_update_rejects_management_items_for_employee_role() -> None:
    service = PreferencesService(InMemoryPushPreferenceRepository(), clock=FrozenClock(NOW))

    with pytest.raises(PreferenceValidationError):
        await service.update(
            TENANT,
            USER,
            Role.EMPLOYEE,
            content_items=("order_progress_summary",),
        )


@pytest.mark.asyncio
async def test_update_rejects_invalid_weekly_cadence() -> None:
    service = PreferencesService(InMemoryPushPreferenceRepository(), clock=FrozenClock(NOW))

    with pytest.raises(PreferenceValidationError):
        await service.update(
            TENANT,
            USER,
            Role.EMPLOYEE,
            weekly_enabled=True,
            weekly_day_of_week=9,
        )


def test_options_are_filtered_by_role() -> None:
    service = PreferencesService(InMemoryPushPreferenceRepository(), clock=FrozenClock(NOW))

    employee = {item["item_id"] for item in service.options_for_role(Role.EMPLOYEE)}
    owner = {item["item_id"] for item in service.options_for_role(Role.OWNER)}

    assert employee == {"wage_detail_push"}
    assert owner >= {"wage_detail_push", "completion_overview", "weekly_output_summary"}
