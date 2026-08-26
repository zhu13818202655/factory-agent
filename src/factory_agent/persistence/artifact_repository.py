"""SQLAlchemy artifact metadata repository (Story 6).

Only the opaque object key and approved metadata are stored: tenant/user owner,
capability, filename, size, SHA-256, and retention timestamps. No sensitive
field (employee ID, name, question text, or amount) is ever persisted.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.persistence.tables import artifact_table
from factory_agent.ports.artifacts import ArtifactRecord
from factory_agent.ports.session import InteractionOwner


class SqlArtifactRepository:
    """PostgreSQL-backed artifact metadata store."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save(self, record: ArtifactRecord) -> None:
        values = _values(record)
        statement = pg_insert(artifact_table).values(**values)
        statement = statement.on_conflict_do_nothing(index_elements=["artifact_id"])
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def get(self, owner: InteractionOwner, artifact_id: str) -> ArtifactRecord | None:
        statement = sa.select(artifact_table).where(
            artifact_table.c.artifact_id == artifact_id,
            artifact_table.c.tenant_id == str(owner.tenant_id),
            artifact_table.c.user_id == str(owner.user_id),
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        return _record_from_row(row) if row is not None else None

    async def delete(self, artifact_id: str) -> None:
        statement = sa.delete(artifact_table).where(artifact_table.c.artifact_id == artifact_id)
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def list_expired(self, now: datetime) -> tuple[ArtifactRecord, ...]:
        statement = sa.select(artifact_table).where(artifact_table.c.expires_at <= now)
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return tuple(_record_from_row(row) for row in rows)


def _values(record: ArtifactRecord) -> dict[str, object]:
    return {
        "artifact_id": record.artifact_id,
        "tenant_id": str(record.tenant_id),
        "user_id": str(record.user_id),
        "interaction_id": record.interaction_id,
        "capability_id": str(record.capability_id),
        "object_key": record.object_key,
        "filename": record.filename,
        "content_type": record.content_type,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
    }


def _record_from_row(row: sa.RowMapping) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        tenant_id=TenantId(row["tenant_id"]),
        user_id=UserId(row["user_id"]),
        interaction_id=row["interaction_id"],
        capability_id=CapabilityId(row["capability_id"]),
        object_key=row["object_key"],
        filename=row["filename"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


__all__ = ["SqlArtifactRepository"]
