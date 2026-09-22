"""SQLAlchemy implementations of the session store.

``commit`` writes the interaction state, its messages, and its SSE events in
one business transaction, then hands the usage events to the metering store.
Metering writes happen in a separate transaction whose failures are isolated:
failures are alerted and never roll back or block the answer.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from factory_agent.domain import (
    CapabilityId,
    ConversationRecord,
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
from factory_agent.observability.debug_trace import drain_debug_captures
from factory_agent.persistence import queries
from factory_agent.persistence.debug_trace_store import SqlDebugTraceStore
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.persistence.tables import (
    event_table,
    interaction_table,
    message_table,
)
from factory_agent.ports import (
    ConversationCreation,
    ConversationPage,
    ConversationSummary,
    InteractionCommit,
    InteractionOwner,
    InteractionPage,
    MessagePage,
)


class SqlInteractionStore:
    """Durable session store; every query is ownership-filtered."""

    def __init__(
        self,
        engine: AsyncEngine,
        metering: SqlMeteringStore | None = None,
        debug_trace: SqlDebugTraceStore | None = None,
    ) -> None:
        self._engine = engine
        self._metering = metering or SqlMeteringStore(engine)
        self._debug_trace = debug_trace

    async def commit(self, commit: InteractionCommit) -> None:
        record = commit.interaction
        async with self._engine.begin() as connection:
            if commit.lifecycle:
                await self._upsert_interaction(connection, record)
                # The conversation row is maintained in the same transaction as
                # the interaction it belongs to: a client that never calls the
                # create endpoint still gets its session listed, and an
                # interrupted write can never leave a turn whose conversation is
                # invisible.
                await self._refresh_conversation(connection, record)
            else:
                await self._touch_interaction(connection, record)
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
        # The debug channel drains here, next to metering and for the same
        # reason: the captures recorded since the last commit belong to this
        # interaction (the buffer is per-interaction by context variable), and
        # the drain is a context read, so no commit site had to learn about it.
        # Draining unconditionally matters even with no store configured —
        # otherwise a capture-enabled process without a database would keep the
        # payloads alive for the life of the interaction.
        captures = drain_debug_captures()
        if self._debug_trace is not None:
            await self._debug_trace.write(captures)

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

    async def fail_stale_run(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        *,
        stale_before: datetime,
        now: datetime,
        category: str,
    ) -> InteractionRecord | None:
        """Mark an orphaned ``running`` interaction failed; ``None`` if not stale.

        The compare-and-set also reserves the terminal event sequence by bumping
        ``last_event_sequence``; the caller persists the terminal event through
        ``commit`` using the returned record.
        """
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        queries.fail_stale_interaction_run(
                            str(owner.tenant_id),
                            str(owner.user_id),
                            str(interaction_id),
                            stale_before=stale_before,
                            now=now,
                            category=category,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _interaction_from_row(row) if row is not None else None

    async def fail_stale_runs(
        self,
        *,
        stale_before: datetime,
        now: datetime,
        category: str,
    ) -> tuple[InteractionRecord, ...]:
        """Bulk startup sweep: fail every stale ``running`` interaction."""
        async with self._engine.begin() as connection:
            rows = (
                (
                    await connection.execute(
                        queries.fail_stale_interaction_runs(
                            stale_before=stale_before,
                            now=now,
                            category=category,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_interaction_from_row(row) for row in rows)

    async def fail_abandoned_runs(
        self,
        *,
        abandoned_before: datetime,
        now: datetime,
        category: str,
    ) -> tuple[InteractionRecord, ...]:
        """Bulk recovery: fail every ``pending`` interaction that never started."""
        async with self._engine.begin() as connection:
            rows = (
                (
                    await connection.execute(
                        queries.fail_abandoned_interaction_runs(
                            abandoned_before=abandoned_before,
                            now=now,
                            category=category,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_interaction_from_row(row) for row in rows)

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
        exclude_kinds: frozenset[MessageKind] = frozenset(),
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
                            tuple(sorted(kind.value for kind in exclude_kinds)),
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

    async def latest_message(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        *,
        kinds: frozenset[MessageKind],
    ) -> MessageRecord | None:
        if not kinds:
            return None
        statement = queries.select_latest_message(
            str(owner.tenant_id),
            str(owner.user_id),
            str(session_id),
            tuple(sorted(kind.value for kind in kinds)),
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None
        return _message_from_row(row)

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

    async def list_conversations(
        self,
        owner: InteractionOwner,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        decoded = queries.decode_cursor(cursor) if cursor else None
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        queries.select_conversations(
                            str(owner.tenant_id), str(owner.user_id), limit, decoded
                        )
                    )
                )
                .mappings()
                .all()
            )
            page = rows[:limit]
            items = await self._hydrate_conversations(
                connection, owner, tuple(_conversation_from_row(row) for row in page)
            )
        next_cursor = (
            queries.encode_cursor(page[-1]["updated_at"], str(page[-1]["session_id"]))
            if len(rows) > limit and page
            else None
        )
        return ConversationPage(items=items, next_cursor=next_cursor)

    async def get_conversation(
        self, owner: InteractionOwner, session_id: SessionId
    ) -> ConversationSummary | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        queries.select_conversation(
                            str(owner.tenant_id), str(owner.user_id), str(session_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            items = await self._hydrate_conversations(
                connection, owner, (_conversation_from_row(row),)
            )
        return items[0]

    async def count_conversations(self, owner: InteractionOwner) -> int:
        async with self._engine.connect() as connection:
            total = (
                await connection.execute(
                    queries.count_conversations(str(owner.tenant_id), str(owner.user_id))
                )
            ).scalar_one()
        return int(total)

    async def create_conversation(
        self, owner: InteractionOwner, session_id: SessionId, now: datetime
    ) -> ConversationCreation:
        tenant_id = str(owner.tenant_id)
        user_id = str(owner.user_id)
        async with self._engine.begin() as connection:
            # ``RETURNING`` rather than ``rowcount``: a conflicting insert
            # reports zero affected rows either way, so the returned row is the
            # only dependable "I created it" signal.
            inserted = (
                await connection.execute(
                    queries.insert_conversation(
                        tenant_id, user_id, str(session_id), created_at=now
                    )
                )
            ).scalar_one_or_none()
            row = (
                (
                    await connection.execute(
                        queries.select_conversation(tenant_id, user_id, str(session_id))
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                # Only reachable if the row disappeared between the insert and
                # the read, which no current code path can do.
                raise RuntimeError("conversation row is missing right after its upsert")
            items = await self._hydrate_conversations(
                connection, owner, (_conversation_from_row(row),)
            )
        return ConversationCreation(summary=items[0], created=inserted is not None)

    async def _hydrate_conversations(
        self,
        connection: AsyncConnection,
        owner: InteractionOwner,
        records: Sequence[ConversationRecord],
    ) -> tuple[ConversationSummary, ...]:
        """Attach the page's aggregates in a constant number of statements.

        Three grouped reads cover every conversation on the page — newest
        message plus its count, the earliest user question, and the newest
        interaction plus its count — so the cost does not grow with the page
        size and no conversation is queried on its own.
        """
        if not records:
            return ()
        tenant_id = str(owner.tenant_id)
        user_id = str(owner.user_id)
        session_ids = tuple(str(record.session_id) for record in records)
        message_rows = (
            (
                await connection.execute(
                    queries.select_conversation_messages(tenant_id, user_id, session_ids)
                )
            )
            .mappings()
            .all()
        )
        title_rows = (
            (
                await connection.execute(
                    queries.select_conversation_first_questions(tenant_id, user_id, session_ids)
                )
            )
            .mappings()
            .all()
        )
        turn_rows = (
            (
                await connection.execute(
                    queries.select_conversation_interactions(tenant_id, user_id, session_ids)
                )
            )
            .mappings()
            .all()
        )
        messages = {str(row["session_id"]): row for row in message_rows}
        titles = {str(row["session_id"]): str(row["text"]) for row in title_rows}
        turns = {str(row["session_id"]): row for row in turn_rows}
        summaries: list[ConversationSummary] = []
        for record in records:
            key = str(record.session_id)
            message_row = messages.get(key)
            turn_row = turns.get(key)
            summaries.append(
                ConversationSummary(
                    conversation=record,
                    interaction_count=(
                        int(turn_row["interaction_count"]) if turn_row is not None else 0
                    ),
                    message_count=(
                        int(message_row["message_count"]) if message_row is not None else 0
                    ),
                    last_message=(
                        _message_from_row(message_row) if message_row is not None else None
                    ),
                    last_status=(
                        InteractionStatus(str(turn_row["status"])) if turn_row is not None else None
                    ),
                    title_source=titles.get(key),
                )
            )
        return tuple(summaries)

    async def delete_session(self, owner: InteractionOwner, session_id: SessionId) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                queries.delete_session(str(owner.tenant_id), str(owner.user_id), str(session_id))
            )
        return result.rowcount > 0

    async def _refresh_conversation(
        self, connection: AsyncConnection, record: InteractionRecord
    ) -> None:
        """Keep the conversation row present and recent, inside the caller's tx.

        A touch first (the steady state: the row already exists and only its
        recency moves), then a create when nothing was updated. The create is
        ``ON CONFLICT DO NOTHING``, so two concurrent first turns cannot
        duplicate the row.
        """
        tenant_id = str(record.tenant_id)
        user_id = str(record.user_id)
        session_id = str(record.session_id)
        touched = await connection.execute(
            queries.touch_conversation(tenant_id, user_id, session_id, now=record.updated_at)
        )
        if touched.rowcount == 0:
            await connection.execute(
                queries.insert_conversation(
                    tenant_id, user_id, session_id, created_at=record.created_at
                )
            )

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

    async def _touch_interaction(
        self, connection: AsyncConnection, record: InteractionRecord
    ) -> None:
        await connection.execute(
            queries.touch_interaction_run(
                str(record.tenant_id),
                str(record.user_id),
                str(record.interaction_id),
                last_event_sequence=record.last_event_sequence,
                updated_at=record.updated_at,
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


def _conversation_from_row(row: RowMapping) -> ConversationRecord:
    return ConversationRecord(
        session_id=SessionId(str(row["session_id"])),
        tenant_id=TenantId(str(row["tenant_id"])),
        user_id=UserId(str(row["user_id"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


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
