"""SQLAlchemy implementations of the history, favorite, and user-mapping stores.

Every durable read and write carries the trusted ``(tenant_id, user_id)``
ownership pair; there is deliberately no "by id only" access path, matching the
session store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.persistence import queries
from factory_agent.persistence.tables import (
    favorite_table,
    query_history_table,
    user_mapping_table,
)
from factory_agent.ports.personal import (
    Favorite,
    HistoryEntry,
    HistoryPage,
    UserMapping,
)


def _owned(table: sa.Table, tenant_id: str, user_id: str) -> sa.ColumnElement[bool]:
    return sa.and_(table.c.tenant_id == tenant_id, table.c.user_id == user_id)


class SqlUserMappingRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(self, mapping: UserMapping) -> None:
        statement = pg_insert(user_mapping_table).values(
            uid=mapping.uid,
            tenant_id=str(mapping.tenant_id),
            uname=mapping.uname,
            company=mapping.company,
            updated_at=mapping.updated_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[user_mapping_table.c.uid, user_mapping_table.c.tenant_id],
            set_={
                "uname": statement.excluded.uname,
                "company": statement.excluded.company,
                "updated_at": statement.excluded.updated_at,
            },
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def get(self, tenant_id: TenantId, uid: str) -> UserMapping | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(user_mapping_table).where(
                            user_mapping_table.c.tenant_id == str(tenant_id),
                            user_mapping_table.c.uid == uid,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _mapping_from_row(row) if row is not None else None

    async def list_for_tenant(self, tenant_id: TenantId) -> tuple[UserMapping, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(user_mapping_table).where(
                            user_mapping_table.c.tenant_id == str(tenant_id)
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_mapping_from_row(row) for row in rows)


class SqlHistoryRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, entry: HistoryEntry) -> None:
        statement = pg_insert(query_history_table).values(
            history_id=entry.history_id,
            tenant_id=str(entry.tenant_id),
            user_id=str(entry.user_id),
            capability_id=str(entry.capability_id),
            intent=dict(entry.intent),
            status=entry.status,
            created_at=entry.created_at,
        )
        statement = statement.on_conflict_do_nothing(
            index_elements=[query_history_table.c.history_id]
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def list(
        self,
        tenant_id: TenantId,
        user_id: UserId,
        limit: int,
        cursor: str | None = None,
    ) -> HistoryPage:
        decoded = queries.decode_cursor(cursor) if cursor else None
        async with self._engine.connect() as connection:
            rows = (
                (await connection.execute(self._select(tenant_id, user_id, limit, decoded)))
                .mappings()
                .all()
            )
        page = rows[:limit]
        next_cursor = (
            queries.encode_cursor(page[-1]["created_at"], str(page[-1]["history_id"]))
            if len(rows) > limit and page
            else None
        )
        return HistoryPage(
            items=tuple(_history_from_row(row) for row in page), next_cursor=next_cursor
        )

    async def delete(self, tenant_id: TenantId, user_id: UserId, history_id: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                sa.delete(query_history_table).where(
                    _owned(query_history_table, str(tenant_id), str(user_id)),
                    query_history_table.c.history_id == history_id,
                )
            )
        return result.rowcount > 0

    def _select(
        self,
        tenant_id: TenantId,
        user_id: UserId,
        limit: int,
        cursor: tuple[datetime, str] | None,
    ) -> sa.Select[Any]:
        statement = sa.select(query_history_table).where(
            _owned(query_history_table, str(tenant_id), str(user_id))
        )
        if cursor is not None:
            created_at, history_id = cursor
            statement = statement.where(
                sa.tuple_(query_history_table.c.created_at, query_history_table.c.history_id)
                > sa.tuple_(sa.literal(created_at), sa.literal(history_id))
            )
        return statement.order_by(
            query_history_table.c.created_at.asc(), query_history_table.c.history_id.asc()
        ).limit(limit + 1)


class SqlFavoriteRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save(self, favorite: Favorite) -> None:
        statement = pg_insert(favorite_table).values(
            favorite_id=favorite.favorite_id,
            tenant_id=str(favorite.tenant_id),
            user_id=str(favorite.user_id),
            capability_id=str(favorite.capability_id),
            title=favorite.title,
            slots=dict(favorite.slots),
            created_at=favorite.created_at,
            expires_at=favorite.expires_at,
        )
        statement = statement.on_conflict_do_nothing(index_elements=[favorite_table.c.favorite_id])
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def get(self, tenant_id: TenantId, user_id: UserId, favorite_id: str) -> Favorite | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(favorite_table).where(
                            _owned(favorite_table, str(tenant_id), str(user_id)),
                            favorite_table.c.favorite_id == favorite_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _favorite_from_row(row) if row is not None else None

    async def list(self, tenant_id: TenantId, user_id: UserId, limit: int) -> tuple[Favorite, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(favorite_table)
                        .where(_owned(favorite_table, str(tenant_id), str(user_id)))
                        .order_by(favorite_table.c.created_at.desc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_favorite_from_row(row) for row in rows)

    async def delete(self, tenant_id: TenantId, user_id: UserId, favorite_id: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                sa.delete(favorite_table).where(
                    _owned(favorite_table, str(tenant_id), str(user_id)),
                    favorite_table.c.favorite_id == favorite_id,
                )
            )
        return result.rowcount > 0

    async def list_expired(self, now: datetime) -> tuple[Favorite, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(favorite_table).where(favorite_table.c.expires_at <= now)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_favorite_from_row(row) for row in rows)


def _mapping_from_row(row: Any) -> UserMapping:
    return UserMapping(
        uid=str(row["uid"]),
        tenant_id=TenantId(str(row["tenant_id"])),
        uname=str(row["uname"]),
        company=row["company"] if isinstance(row["company"], str) else None,
        updated_at=row["updated_at"],
    )


def _history_from_row(row: Any) -> HistoryEntry:
    return HistoryEntry(
        history_id=str(row["history_id"]),
        tenant_id=TenantId(str(row["tenant_id"])),
        user_id=UserId(str(row["user_id"])),
        capability_id=CapabilityId(str(row["capability_id"])),
        intent=dict(row["intent"]),
        status=str(row["status"]),
        created_at=row["created_at"],
    )


def _favorite_from_row(row: Any) -> Favorite:
    return Favorite(
        favorite_id=str(row["favorite_id"]),
        tenant_id=TenantId(str(row["tenant_id"])),
        user_id=UserId(str(row["user_id"])),
        capability_id=CapabilityId(str(row["capability_id"])),
        title=str(row["title"]),
        slots=dict(row["slots"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


__all__ = [
    "SqlFavoriteRepository",
    "SqlHistoryRepository",
    "SqlUserMappingRepository",
]
