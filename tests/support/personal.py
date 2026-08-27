"""In-memory history, favorite, and user-mapping repositories for tests.

These mirror the ownership semantics of the SQL stores: a record owned by
another tenant/user is indistinguishable from one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from factory_agent.domain import TenantId, UserId
from factory_agent.ports.personal import (
    Favorite,
    HistoryEntry,
    HistoryPage,
    UserMapping,
)


@dataclass
class InMemoryHistoryRepository:
    entries: list[HistoryEntry] = field(default_factory=list[HistoryEntry])

    async def record(self, entry: HistoryEntry) -> None:
        self.entries = [
            existing for existing in self.entries if existing.history_id != entry.history_id
        ]
        self.entries.append(entry)

    async def list(
        self,
        tenant_id: TenantId,
        user_id: UserId,
        limit: int,
        cursor: str | None = None,
    ) -> HistoryPage:
        owned = [
            entry
            for entry in self.entries
            if entry.tenant_id == tenant_id and entry.user_id == user_id
        ]
        owned.sort(key=lambda entry: (entry.created_at, entry.history_id))
        page = tuple(owned[:limit])
        return HistoryPage(items=page, next_cursor=None)

    async def delete(self, tenant_id: TenantId, user_id: UserId, history_id: str) -> bool:
        before = len(self.entries)
        self.entries = [
            entry
            for entry in self.entries
            if not (
                entry.history_id == history_id
                and entry.tenant_id == tenant_id
                and entry.user_id == user_id
            )
        ]
        return len(self.entries) < before


@dataclass
class InMemoryFavoriteRepository:
    favorites: list[Favorite] = field(default_factory=list[Favorite])

    async def save(self, favorite: Favorite) -> None:
        self.favorites = [f for f in self.favorites if f.favorite_id != favorite.favorite_id]
        self.favorites.append(favorite)

    async def get(self, tenant_id: TenantId, user_id: UserId, favorite_id: str) -> Favorite | None:
        for favorite in self.favorites:
            if (
                favorite.favorite_id == favorite_id
                and favorite.tenant_id == tenant_id
                and favorite.user_id == user_id
            ):
                return favorite
        return None

    async def list(self, tenant_id: TenantId, user_id: UserId, limit: int) -> tuple[Favorite, ...]:
        owned = [
            favorite
            for favorite in self.favorites
            if favorite.tenant_id == tenant_id and favorite.user_id == user_id
        ]
        owned.sort(key=lambda favorite: favorite.created_at, reverse=True)
        return tuple(owned[:limit])

    async def delete(self, tenant_id: TenantId, user_id: UserId, favorite_id: str) -> bool:
        before = len(self.favorites)
        self.favorites = [
            favorite
            for favorite in self.favorites
            if not (
                favorite.favorite_id == favorite_id
                and favorite.tenant_id == tenant_id
                and favorite.user_id == user_id
            )
        ]
        return len(self.favorites) < before

    async def list_expired(self, now: datetime) -> tuple[Favorite, ...]:
        return tuple(favorite for favorite in self.favorites if favorite.expires_at <= now)


@dataclass
class InMemoryUserMappingRepository:
    mappings: dict[tuple[str, str], UserMapping] = field(
        default_factory=dict[tuple[str, str], UserMapping]
    )

    async def upsert(self, mapping: UserMapping) -> None:
        self.mappings[(str(mapping.tenant_id), mapping.uid)] = mapping

    async def get(self, tenant_id: TenantId, uid: str) -> UserMapping | None:
        return self.mappings.get((str(tenant_id), uid))

    async def list_for_tenant(self, tenant_id: TenantId) -> tuple[UserMapping, ...]:
        return tuple(
            mapping
            for (mapping_tenant, _uid), mapping in self.mappings.items()
            if mapping_tenant == str(tenant_id)
        )


__all__ = [
    "InMemoryFavoriteRepository",
    "InMemoryHistoryRepository",
    "InMemoryUserMappingRepository",
]
