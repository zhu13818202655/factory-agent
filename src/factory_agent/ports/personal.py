"""History, favorites, and minimal user-mapping contracts.

Every durable read is ownership-filtered by the trusted ``(tenant_id, user_id)``
pair; a record owned by another user is indistinguishable from one that does
not exist. History and favorites carry only normalized, non-sensitive slots —
never raw question text, work numbers, wage/output amounts, or ``DataScope`` ID
lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from factory_agent.domain import CapabilityId, TenantId, UserId


@dataclass(frozen=True, slots=True)
class UserMapping:
    """Minimal ``uid`` ↔ ``uname``/``company`` mapping.

    Stored for session continuity and history display only. It must never enter
    an LLM prompt or a usage event.
    """

    uid: str
    tenant_id: TenantId
    uname: str
    company: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    history_id: str
    tenant_id: TenantId
    user_id: UserId
    capability_id: CapabilityId
    #: Normalized non-sensitive intent (e.g. ``{"time_expression": "本月"}``).
    intent: dict[str, object]
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: tuple[HistoryEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Favorite:
    favorite_id: str
    tenant_id: TenantId
    user_id: UserId
    capability_id: CapabilityId
    title: str
    #: Non-sensitive slots only (time expression, order/plan/style codes).
    slots: dict[str, object]
    created_at: datetime
    expires_at: datetime


class UserMappingRepository(Protocol):
    async def upsert(self, mapping: UserMapping) -> None: ...

    async def get(self, tenant_id: TenantId, uid: str) -> UserMapping | None: ...

    async def list_for_tenant(self, tenant_id: TenantId) -> tuple[UserMapping, ...]: ...


class HistoryRepository(Protocol):
    async def record(self, entry: HistoryEntry) -> None: ...

    async def list(
        self,
        tenant_id: TenantId,
        user_id: UserId,
        limit: int,
        cursor: str | None = None,
    ) -> HistoryPage: ...

    async def delete(self, tenant_id: TenantId, user_id: UserId, history_id: str) -> bool: ...


class FavoriteRepository(Protocol):
    async def save(self, favorite: Favorite) -> None: ...

    async def get(
        self, tenant_id: TenantId, user_id: UserId, favorite_id: str
    ) -> Favorite | None: ...

    async def list(
        self, tenant_id: TenantId, user_id: UserId, limit: int
    ) -> tuple[Favorite, ...]: ...

    async def delete(self, tenant_id: TenantId, user_id: UserId, favorite_id: str) -> bool: ...

    async def list_expired(self, now: datetime) -> tuple[Favorite, ...]: ...


__all__ = [
    "Favorite",
    "FavoriteRepository",
    "HistoryEntry",
    "HistoryPage",
    "HistoryRepository",
    "UserMapping",
    "UserMappingRepository",
]
