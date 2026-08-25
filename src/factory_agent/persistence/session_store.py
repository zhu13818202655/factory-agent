"""SQLAlchemy implementations of the session store and the usage outbox.

``commit`` writes the interaction state, its messages, its SSE events, and the
usage outbox rows in one transaction, so a usage-admin outage can never change
an answer outcome. Publication happens later in a separate process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageId,
    MessageKind,
    MessageRecord,
    MessageRole,
    SessionEvent,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.persistence import queries
from factory_agent.persistence.tables import (
    event_table,
    interaction_table,
    message_table,
    usage_outbox_table,
)
from factory_agent.ports import (
    InteractionCommit,
    InteractionOwner,
    InteractionPage,
    MessagePage,
    OutboxRecord,
)


class SqlInteractionStore:
    """Durable session store; every query is ownership-filtered."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def commit(self, commit: InteractionCommit) -> None:
        record = commit.interaction
        async with self._engine.begin() as connection:
            await self._upsert_interaction(connection, record)
            for message in commit.messages:
                await connection.execute(
                    _upsert(
                        message_table,
                        _message_values(message),
                        (message_table.c.message_id,),
                    )
                )
            for event in commit.events:
                await connection.execute(
                    _upsert(
                        event_table,
                        {
                            "interaction_id": str(record.interaction_id),
                            "sequence": event.sequence,
                            "tenant_id": str(record.tenant_id),
                            "user_id": str(record.user_id),
                            "name": event.name,
                            "data": dict(event.data),
                            "created_at": record.updated_at,
                        },
                        (event_table.c.interaction_id, event_table.c.sequence),
                    )
                )
            for usage_event in commit.usage_events:
                await connection.execute(
                    _upsert(
                        usage_outbox_table,
                        {
                            "event_id": usage_event.event_id,
                            "event_type": usage_event.event_type,
                            "tenant_id": str(usage_event.tenant_id),
                            "payload": dict(usage_event.payload),
                            "created_at": usage_event.created_at,
                            "available_at": usage_event.created_at,
                            "attempts": 0,
                        },
                        (usage_outbox_table.c.event_id,),
                    )
                )

    async def claim_run(
        self, owner: InteractionOwner, interaction_id: InteractionId, now: datetime
    ) -> InteractionRecord | None:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        queries.claim_interaction_run(
                            str(owner.tenant_id), str(owner.user_id), str(interaction_id), now
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _interaction_from_row(row) if row is not None else None

    async def get_interaction(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        queries.select_interaction(
                            str(owner.tenant_id), str(owner.user_id), str(interaction_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _interaction_from_row(row) if row is not None else None

    async def list_events(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        after_sequence: int,
    ) -> tuple[SessionEvent, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        queries.select_events(
                            str(owner.tenant_id),
                            str(owner.user_id),
                            str(interaction_id),
                            after_sequence,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            SessionEvent(
                sequence=int(row["sequence"]),
                name=str(row["name"]),
                data=dict(row["data"]),
            )
            for row in rows
        )

    async def list_messages(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
    ) -> MessagePage:
        decoded = queries.decode_cursor(cursor) if cursor else None
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        queries.select_messages(
                            str(owner.tenant_id),
                            str(owner.user_id),
                            str(session_id),
                            limit,
                            decoded,
                        )
                    )
                )
                .mappings()
                .all()
            )
        page = rows[:limit]
        next_cursor = (
            queries.encode_cursor(page[-1]["created_at"], str(page[-1]["message_id"]))
            if len(rows) > limit and page
            else None
        )
        return MessagePage(
            items=tuple(_message_from_row(row) for row in page), next_cursor=next_cursor
        )

    async def list_interactions(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
    ) -> InteractionPage:
        decoded = queries.decode_cursor(cursor) if cursor else None
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        queries.select_interactions(
                            str(owner.tenant_id),
                            str(owner.user_id),
                            str(session_id),
                            limit,
                            decoded,
                        )
                    )
                )
                .mappings()
                .all()
            )
        page = rows[:limit]
        next_cursor = (
            queries.encode_cursor(page[-1]["created_at"], str(page[-1]["interaction_id"]))
            if len(rows) > limit and page
            else None
        )
        return InteractionPage(
            items=tuple(_interaction_from_row(row) for row in page), next_cursor=next_cursor
        )

    async def delete_session(self, owner: InteractionOwner, session_id: SessionId) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                queries.delete_session(str(owner.tenant_id), str(owner.user_id), str(session_id))
            )
        return result.rowcount > 0

    async def _upsert_interaction(
        self, connection: AsyncConnection, record: InteractionRecord
    ) -> None:
        await connection.execute(
            _upsert(
                interaction_table,
                {
                    "interaction_id": str(record.interaction_id),
                    "session_id": str(record.session_id),
                    "tenant_id": str(record.tenant_id),
                    "user_id": str(record.user_id),
                    "status": record.status.value,
                    "state": record.state.value,
                    "input_text": record.input_text,
                    "capability_id": (
                        str(record.capability_id) if record.capability_id is not None else None
                    ),
                    "clarification_rounds": record.clarification_rounds,
                    "last_event_sequence": record.last_event_sequence,
                    "error_category": record.error_category,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                    "completed_at": record.completed_at,
                },
                (interaction_table.c.interaction_id,),
            )
        )


class SqlUsageOutbox:
    """Outbox reader used only by the independent publisher process."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim(self, limit: int, now: datetime) -> tuple[OutboxRecord, ...]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(queries.claim_outbox(limit, now))).mappings().all()
        return tuple(
            OutboxRecord(
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                tenant_id=TenantId(str(row["tenant_id"])),
                payload=dict(row["payload"]),
                attempts=int(row["attempts"]),
                available_at=row["available_at"],
            )
            for row in rows
        )

    async def mark_published(self, event_ids: tuple[str, ...], now: datetime) -> None:
        if not event_ids:
            return
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.update(usage_outbox_table)
                .where(usage_outbox_table.c.event_id.in_(event_ids))
                .values(published_at=now)
            )

    async def mark_failed(
        self,
        event_ids: tuple[str, ...],
        reason: str,
        retry_at: datetime,
        dead_letter: bool,
    ) -> None:
        if not event_ids:
            return
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.update(usage_outbox_table)
                .where(usage_outbox_table.c.event_id.in_(event_ids))
                .values(
                    attempts=usage_outbox_table.c.attempts + 1,
                    available_at=retry_at,
                    dead_lettered=dead_letter,
                    dead_letter_reason=reason[:200] if dead_letter else None,
                )
            )

    async def backlog_size(self, now: datetime) -> int:
        async with self._engine.connect() as connection:
            value = await connection.scalar(queries.count_backlog(now))
        return int(value or 0)


def _upsert(
    table: sa.Table,
    values: dict[str, Any],
    conflict_columns: tuple[sa.Column[Any], ...],
) -> sa.Executable:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    statement = pg_insert(table).values(values)
    updatable = {
        name: statement.excluded[name]
        for name in values
        if name not in {column.name for column in conflict_columns}
    }
    if not updatable:
        return statement.on_conflict_do_nothing(
            index_elements=[column.name for column in conflict_columns]
        )
    return statement.on_conflict_do_update(
        index_elements=[column.name for column in conflict_columns], set_=updatable
    )


def _message_values(message: MessageRecord) -> dict[str, Any]:
    return {
        "message_id": str(message.message_id),
        "interaction_id": str(message.interaction_id),
        "session_id": str(message.session_id),
        "tenant_id": str(message.tenant_id),
        "user_id": str(message.user_id),
        "role": message.role.value,
        "kind": message.kind.value,
        "sequence": message.sequence,
        "text": message.text,
        "payload": dict(message.payload),
        "created_at": message.created_at,
    }


def _interaction_from_row(row: RowMapping) -> InteractionRecord:
    capability = row["capability_id"]
    return InteractionRecord(
        interaction_id=InteractionId(str(row["interaction_id"])),
        session_id=SessionId(str(row["session_id"])),
        tenant_id=TenantId(str(row["tenant_id"])),
        user_id=UserId(str(row["user_id"])),
        status=InteractionStatus(str(row["status"])),
        state=SessionState(str(row["state"])),
        input_text=str(row["input_text"]),
        capability_id=CapabilityId(str(capability)) if capability else None,
        clarification_rounds=int(row["clarification_rounds"]),
        last_event_sequence=int(row["last_event_sequence"]),
        error_category=row["error_category"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def _message_from_row(row: RowMapping) -> MessageRecord:
    return MessageRecord(
        message_id=MessageId(str(row["message_id"])),
        interaction_id=InteractionId(str(row["interaction_id"])),
        session_id=SessionId(str(row["session_id"])),
        tenant_id=TenantId(str(row["tenant_id"])),
        user_id=UserId(str(row["user_id"])),
        role=MessageRole(str(row["role"])),
        kind=MessageKind(str(row["kind"])),
        sequence=int(row["sequence"]),
        text=str(row["text"]),
        payload=dict(row["payload"]),
        created_at=row["created_at"],
    )


__all__ = ["SqlInteractionStore", "SqlUsageOutbox"]
