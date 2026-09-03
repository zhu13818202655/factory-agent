"""SQLAlchemy push stores (Story 3B): preferences + delivery log."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.domain import TenantId, UserId
from factory_agent.persistence.tables import push_delivery_table, user_preference_table
from factory_agent.ports.push import PushDelivery
from factory_agent.ports.push_preferences import PushPreferences


class SqlPushPreferenceRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, tenant_id: TenantId, user_id: UserId) -> PushPreferences | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(user_preference_table).where(
                            user_preference_table.c.tenant_id == str(tenant_id),
                            user_preference_table.c.user_id == str(user_id),
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return PushPreferences(
            tenant_id=tenant_id,
            user_id=user_id,
            weekly_enabled=bool(row["weekly_enabled"]),
            weekly_day_of_week=row["weekly_day_of_week"],
            weekly_time=row["weekly_time"],
            monthly_enabled=bool(row["monthly_enabled"]),
            monthly_day_of_month=row["monthly_day_of_month"],
            monthly_time=row["monthly_time"],
            content_items=tuple(row["content_items"] or ()),
        )

    async def upsert(self, prefs: PushPreferences) -> None:
        statement = pg_insert(user_preference_table).values(
            tenant_id=str(prefs.tenant_id),
            user_id=str(prefs.user_id),
            weekly_enabled=prefs.weekly_enabled,
            weekly_day_of_week=prefs.weekly_day_of_week,
            weekly_time=prefs.weekly_time,
            monthly_enabled=prefs.monthly_enabled,
            monthly_day_of_month=prefs.monthly_day_of_month,
            monthly_time=prefs.monthly_time,
            content_items=list(prefs.content_items),
            updated_at=datetime.now().astimezone(),
        )
        statement = statement.on_conflict_do_update(
            constraint="agent_user_preference_pkey",
            set_={
                "weekly_enabled": statement.excluded.weekly_enabled,
                "weekly_day_of_week": statement.excluded.weekly_day_of_week,
                "weekly_time": statement.excluded.weekly_time,
                "monthly_enabled": statement.excluded.monthly_enabled,
                "monthly_day_of_month": statement.excluded.monthly_day_of_month,
                "monthly_time": statement.excluded.monthly_time,
                "content_items": statement.excluded.content_items,
                "updated_at": statement.excluded.updated_at,
            },
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)


class SqlPushDeliveryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, delivery: PushDelivery) -> None:
        statement = sa.insert(push_delivery_table).values(
            delivery_id=delivery.delivery_id,
            tenant_id=str(delivery.tenant_id),
            user_id=str(delivery.user_id),
            kind=delivery.kind,
            content_item_id=delivery.content_item_id,
            status=delivery.status,
            message_digest=delivery.message_digest,
            row_count=delivery.row_count,
            created_at=delivery.created_at,
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)


__all__ = ["SqlPushDeliveryStore", "SqlPushPreferenceRepository"]
