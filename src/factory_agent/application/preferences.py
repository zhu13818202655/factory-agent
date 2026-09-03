"""Preferences service (Story 3B): read/update + role-filtered options."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from factory_agent.application.push_catalog import (
    content_item_ids_for_role,
    content_items_for_role,
)
from factory_agent.domain import Role, TenantId, UserId
from factory_agent.ports.push_preferences import (
    PushPreferenceRepository,
    PushPreferences,
    default_preferences,
    validate_preferences,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class PreferenceValidationError(ValueError):
    """Raised when an update violates role scope or cadence rules."""


class PreferencesService:
    """Owns per-user push preferences; the morning report is never configurable."""

    def __init__(
        self,
        repository: PushPreferenceRepository | None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or _SystemClock()

    async def get(self, tenant_id: TenantId, user_id: UserId) -> PushPreferences:
        if self._repository is None:
            return default_preferences(tenant_id, user_id)
        stored = await self._repository.get(tenant_id, user_id)
        return stored or default_preferences(tenant_id, user_id)

    async def update(
        self,
        tenant_id: TenantId,
        user_id: UserId,
        role: Role,
        *,
        weekly_enabled: bool | None = None,
        weekly_day_of_week: int | None = None,
        weekly_time: str | None = None,
        monthly_enabled: bool | None = None,
        monthly_day_of_month: int | None = None,
        monthly_time: str | None = None,
        content_items: tuple[str, ...] | None = None,
    ) -> PushPreferences:
        if self._repository is None:
            raise PreferenceValidationError("push preferences are not configured")
        current = await self.get(tenant_id, user_id)
        allowed = content_item_ids_for_role(role)
        selected = (
            tuple(item for item in content_items if item in allowed)
            if content_items is not None
            else current.content_items
        )
        if content_items is not None and any(item not in allowed for item in content_items):
            raise PreferenceValidationError("包含当前角色不可订阅的推送项")
        updated = PushPreferences(
            tenant_id=tenant_id,
            user_id=user_id,
            weekly_enabled=(current.weekly_enabled if weekly_enabled is None else weekly_enabled),
            weekly_day_of_week=(
                current.weekly_day_of_week if weekly_day_of_week is None else weekly_day_of_week
            ),
            weekly_time=current.weekly_time if weekly_time is None else weekly_time,
            monthly_enabled=(
                current.monthly_enabled if monthly_enabled is None else monthly_enabled
            ),
            monthly_day_of_month=(
                current.monthly_day_of_month
                if monthly_day_of_month is None
                else monthly_day_of_month
            ),
            monthly_time=current.monthly_time if monthly_time is None else monthly_time,
            content_items=selected,
        )
        message = validate_preferences(updated)
        if message is not None:
            raise PreferenceValidationError(message)
        await self._repository.upsert(updated)
        return updated

    def options_for_role(self, role: Role) -> list[dict[str, str]]:
        """Role-filtered selectable content items."""
        return [
            {
                "item_id": item.item_id,
                "title": item.title,
                "capability_id": item.capability.value,
            }
            for item in content_items_for_role(role)
        ]


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


__all__ = ["PreferencesService", "PreferenceValidationError"]
