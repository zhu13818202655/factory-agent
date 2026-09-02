"""SQLAlchemy implementations of the session store.

``commit`` writes the interaction state, its messages, and its SSE events in
one business transaction, then hands the usage events to the metering store.
Metering writes happen in a separate transaction whose failures are isolated:
failures are alerted and never roll back or block the answer.
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
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.persistence.tables import (
    event_table,
    interaction_table,
    message_table,
)
from factory_agent.ports import (
    InteractionCommit,
    InteractionOwner,
    InteractionPage,
    MessagePage,
)


class SqlInteractionStore:
    """Durable session store; every query is ownership-filtered."""

    def __init__(self, engine: AsyncEngine, metering: SqlMeteringStore | None = None) -> None:
        self._engine = engine
        self._metering = metering or SqlMeteringStore(engine)

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
        # Metering is a separate transaction so a write failure can never roll
        # back the business data above.
        await self._metering.write_usage_events(commit.usage_events)

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


__all__ = ["SqlInteractionStore"]
