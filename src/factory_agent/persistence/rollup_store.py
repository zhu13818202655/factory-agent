"""SQLAlchemy rollup store: fact reads and idempotent rollup upserts.

Owns reads from the three fact tables and writes to ``tenant_usage_hourly`` /
``tenant_usage_daily``. ``mes_operation_category`` is read to
classify MES calls at aggregation time; the classification is never stored in
the events themselves.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.persistence.tables import (
    interaction_fact_table,
    llm_call_fact_table,
    mes_call_fact_table,
    mes_operation_category_table,
    tenant_usage_daily_table,
    tenant_usage_hourly_table,
)
from factory_agent.ports.rollup import (
    InteractionFactRow,
    LlmCallFactRow,
    MesCallFactRow,
    RollupRow,
)


class SqlRollupStore:
    """PostgreSQL rollup store over the fact and rollup tables."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_facts(
        self,
        tenant_ids: frozenset[str],
        start: datetime,
        end: datetime,
    ) -> tuple[list[InteractionFactRow], list[LlmCallFactRow], list[MesCallFactRow]]:
        async with self._engine.connect() as connection:
            interaction_rows = (
                (
                    await connection.execute(
                        sa.select(
                            interaction_fact_table.c.tenant_id,
                            interaction_fact_table.c.occurred_at,
                            interaction_fact_table.c.event_type,
                            interaction_fact_table.c.user_subject_id,
                            interaction_fact_table.c.capability_id,
                            interaction_fact_table.c.status,
                            interaction_fact_table.c.duration_ms,
                            interaction_fact_table.c.mes_duration_ms,
                            interaction_fact_table.c.llm_duration_ms,
                            interaction_fact_table.c.local_duration_ms,
                        ).where(
                            interaction_fact_table.c.tenant_id.in_(tuple(tenant_ids)),
                            interaction_fact_table.c.occurred_at >= start,
                            interaction_fact_table.c.occurred_at < end,
                        )
                    )
                )
                .mappings()
                .all()
            )
            llm_rows = (
                (
                    await connection.execute(
                        sa.select(
                            llm_call_fact_table.c.tenant_id,
                            llm_call_fact_table.c.occurred_at,
                            llm_call_fact_table.c.logical_call_id,
                            llm_call_fact_table.c.prompt_tokens,
                            llm_call_fact_table.c.completion_tokens,
                            llm_call_fact_table.c.cached_tokens,
                            llm_call_fact_table.c.reasoning_tokens,
                        ).where(
                            llm_call_fact_table.c.tenant_id.in_(tuple(tenant_ids)),
                            llm_call_fact_table.c.occurred_at >= start,
                            llm_call_fact_table.c.occurred_at < end,
                        )
                    )
                )
                .mappings()
                .all()
            )
            mes_rows = (
                (
                    await connection.execute(
                        sa.select(
                            mes_call_fact_table.c.tenant_id,
                            mes_call_fact_table.c.occurred_at,
                            mes_call_fact_table.c.operation_id,
                            mes_call_fact_table.c.status,
                        ).where(
                            mes_call_fact_table.c.tenant_id.in_(tuple(tenant_ids)),
                            mes_call_fact_table.c.occurred_at >= start,
                            mes_call_fact_table.c.occurred_at < end,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return (
            [
                InteractionFactRow(
                    tenant_id=str(row["tenant_id"]),
                    occurred_at=row["occurred_at"],
                    event_type=str(row["event_type"]),
                    user_subject_id=str(row["user_subject_id"]),
                    capability_id=row["capability_id"],
                    status=row["status"],
                    duration_ms=_int_or_none(row["duration_ms"]),
                    mes_duration_ms=_int_or_none(row["mes_duration_ms"]),
                    llm_duration_ms=_int_or_none(row["llm_duration_ms"]),
                    local_duration_ms=_int_or_none(row["local_duration_ms"]),
                )
                for row in interaction_rows
            ],
            [
                LlmCallFactRow(
                    tenant_id=str(row["tenant_id"]),
                    occurred_at=row["occurred_at"],
                    logical_call_id=str(row["logical_call_id"]),
                    prompt_tokens=_int(row["prompt_tokens"]),
                    completion_tokens=_int(row["completion_tokens"]),
                    cached_tokens=_int(row["cached_tokens"]),
                    reasoning_tokens=_int(row["reasoning_tokens"]),
                )
                for row in llm_rows
            ],
            [
                MesCallFactRow(
                    tenant_id=str(row["tenant_id"]),
                    occurred_at=row["occurred_at"],
                    operation_id=str(row["operation_id"]),
                    status=str(row["status"]),
                )
                for row in mes_rows
            ],
        )

    async def list_mes_categories(self) -> dict[str, str]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(
                            mes_operation_category_table.c.operation_id,
                            mes_operation_category_table.c.category,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {str(row["operation_id"]): str(row["category"]) for row in rows}

    async def upsert_rollup_rows(self, rows: list[RollupRow]) -> None:
        if not rows:
            return
        async with self._engine.begin() as connection:
            for row in rows:
                if row.granularity == "hour":
                    statement = pg_insert(tenant_usage_hourly_table).values(
                        tenant_id=row.tenant_id,
                        bucket_start=row.bucket_start,
                        metric=row.metric,
                        value=row.value,
                        rollup_version=row.rollup_version,
                        rolled_up_at=row.rolled_up_at,
                    )
                    statement = statement.on_conflict_do_update(
                        index_elements=["tenant_id", "bucket_start", "metric"],
                        set_={
                            "value": row.value,
                            "rollup_version": row.rollup_version,
                            "rolled_up_at": row.rolled_up_at,
                        },
                    )
                else:
                    statement = pg_insert(tenant_usage_daily_table).values(
                        tenant_id=row.tenant_id,
                        bucket_date=row.bucket_start.date(),
                        metric=row.metric,
                        value=row.value,
                        rollup_version=row.rollup_version,
                        rolled_up_at=row.rolled_up_at,
                    )
                    statement = statement.on_conflict_do_update(
                        index_elements=["tenant_id", "bucket_date", "metric"],
                        set_={
                            "value": row.value,
                            "rollup_version": row.rollup_version,
                            "rolled_up_at": row.rolled_up_at,
                        },
                    )
                await connection.execute(statement)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _int(value: object) -> int:
    if isinstance(value, (int, float, str)) and value:
        return int(value)
    return 0


__all__ = [
    "InteractionFactRow",
    "LlmCallFactRow",
    "MesCallFactRow",
    "RollupRow",
    "SqlRollupStore",
]
