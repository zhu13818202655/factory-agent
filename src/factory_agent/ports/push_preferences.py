"""Push subscription preferences (Story 3B).

User-level (tenant+user) preferences for the monthly/weekly pushes. The daily
morning report is default-on and deliberately NOT represented here (不可配置
关闭). Preferences are non-sensitive: dates, times, and content-item ids only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from factory_agent.domain import TenantId, UserId

#: Day-of-week 0=Monday..6=Sunday (ISO).
_WEEKDAYS = frozenset(range(0, 7))


@dataclass(frozen=True, slots=True)
class PushPreferences:
    tenant_id: TenantId
    user_id: UserId
    weekly_enabled: bool = False
    weekly_day_of_week: int | None = None
    weekly_time: str | None = None
    monthly_enabled: bool = False
    monthly_day_of_month: int | None = None
    monthly_time: str | None = None
    content_items: tuple[str, ...] = field(default_factory=tuple)


def default_preferences(tenant_id: TenantId, user_id: UserId) -> PushPreferences:
    return PushPreferences(tenant_id=tenant_id, user_id=user_id)


def validate_preferences(prefs: PushPreferences) -> str | None:
    """Return a friendly validation message, or None when the preferences are OK."""
    if prefs.weekly_enabled:
        if prefs.weekly_day_of_week not in _WEEKDAYS:
            return "周推送需要选择星期（0-6）"
        if not prefs.weekly_time:
            return "周推送需要设置时间"
    if prefs.monthly_enabled:
        if not prefs.monthly_day_of_month or not 1 <= prefs.monthly_day_of_month <= 31:
            return "月推送需要选择日期（1-31）"
        if not prefs.monthly_time:
            return "月推送需要设置时间"
    return None


class PushPreferenceRepository(Protocol):
    async def get(self, tenant_id: TenantId, user_id: UserId) -> PushPreferences | None: ...

    async def upsert(self, prefs: PushPreferences) -> None: ...


__all__ = [
    "PushPreferences",
    "PushPreferenceRepository",
    "default_preferences",
    "validate_preferences",
]
