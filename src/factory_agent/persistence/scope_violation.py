"""SQLAlchemy store for role-consistency violation records (Story 2)."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.persistence.tables import scope_violation_table
from factory_agent.ports.scope_violation import ScopeViolationRecord


class SqlScopeViolationStore:
    """PostgreSQL-backed review surface for consistency findings."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, entry: ScopeViolationRecord) -> None:
        statement = sa.insert(scope_violation_table).values(
            violation_id=entry.violation_id,
            tenant_id=str(entry.tenant_id),
            user_id=str(entry.user_id),
            role=entry.role.value,
            capability_id=entry.capability_id,
            level=entry.level,
            mode=entry.mode,
            reason_code=entry.reason_code,
            interaction_id=entry.interaction_id,
            expected_range=entry.expected_range,
            actual_summary=entry.actual_summary,
            row_count=entry.row_count,
            sample_count=entry.sample_count,
            sample_digests=list(entry.sample_digests),
            created_at=entry.created_at,
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def list(
        self,
        since: datetime,
        limit: int = 1000,
    ) -> tuple[ScopeViolationRecord, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(scope_violation_table)
                        .where(scope_violation_table.c.created_at >= since)
                        .order_by(scope_violation_table.c.created_at.asc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_record_from_row(row) for row in rows)


def _record_from_row(row: sa.RowMapping) -> ScopeViolationRecord:
    from factory_agent.domain import Role

    return ScopeViolationRecord(
        violation_id=row["violation_id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        role=Role(row["role"]),
        capability_id=row["capability_id"],
        level=row["level"],
        mode=row["mode"],
        reason_code=row["reason_code"],
        interaction_id=row["interaction_id"],
        expected_range=row["expected_range"],
        actual_summary=row["actual_summary"],
        row_count=row["row_count"],
        sample_count=row["sample_count"],
        sample_digests=tuple(row["sample_digests"] or ()),
        created_at=row["created_at"],
    )


__all__ = ["SqlScopeViolationStore"]
