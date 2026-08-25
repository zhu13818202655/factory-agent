"""Session store behaviour against a real PostgreSQL database.

The offline proof that every business statement is ownership-filtered lives in
``tests/unit/persistence/test_session_queries.py``. This suite exercises the
same store against a real server so upsert semantics, cursor pagination,
``ON DELETE CASCADE`` and the ``PENDING -> RUNNING`` compare-and-set are proven
against the dialect that production uses.

Set ``FACTORY_AGENT_TEST_POSTGRES_URL`` to a disposable database to enable it.
The suite creates and drops its own schema and never touches customer data.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from factory_agent.domain import (
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
from factory_agent.persistence.engine import normalize_dsn
from factory_agent.persistence.session_store import SqlInteractionStore, SqlUsageOutbox
from factory_agent.persistence.tables import METADATA
from factory_agent.ports import InteractionCommit, InteractionOwner, UsageOutboxEvent

DATABASE_URL = os.environ.get("FACTORY_AGENT_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set FACTORY_AGENT_TEST_POSTGRES_URL to a disposable database to run these tests",
    ),
    pytest.mark.asyncio,
]

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
TENANT = TenantId("tenant-a")
OWNER = InteractionOwner(tenant_id=TENANT, user_id=UserId("user-a"))
INTRUDER = InteractionOwner(tenant_id=TENANT, user_id=UserId("user-b"))
OTHER_TENANT = InteractionOwner(tenant_id=TenantId("tenant-b"), user_id=UserId("user-a"))
SESSION = SessionId("s-1")


def async_url(url: str) -> str:
    return normalize_dsn(url)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert DATABASE_URL is not None
    created = create_async_engine(async_url(DATABASE_URL), poolclass=sa.pool.NullPool)
    async with created.begin() as connection:
        await connection.run_sync(METADATA.drop_all)
        await connection.run_sync(METADATA.create_all)
    try:
        yield created
    finally:
        async with created.begin() as connection:
            await connection.run_sync(METADATA.drop_all)
        await created.dispose()


@pytest.fixture
def store(engine: AsyncEngine) -> SqlInteractionStore:
    return SqlInteractionStore(engine)


def interaction(
    interaction_id: str,
    *,
    owner: InteractionOwner = OWNER,
    session_id: SessionId = SESSION,
    status: InteractionStatus = InteractionStatus.PENDING,
    state: SessionState = SessionState.PARSING,
    created_at: datetime = NOW,
    last_event_sequence: int = 0,
) -> InteractionRecord:
    return InteractionRecord(
        interaction_id=InteractionId(interaction_id),
        session_id=session_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        status=status,
        state=state,
        input_text="上个月我的产量",
        capability_id=None,
        clarification_rounds=0,
        last_event_sequence=last_event_sequence,
        error_category=None,
        created_at=created_at,
        updated_at=created_at,
        completed_at=None,
    )


def message(
    message_id: str,
    interaction_id: str,
    sequence: int,
    *,
    owner: InteractionOwner = OWNER,
    created_at: datetime = NOW,
) -> MessageRecord:
    return MessageRecord(
        message_id=MessageId(message_id),
        interaction_id=InteractionId(interaction_id),
        session_id=SESSION,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        role=MessageRole.USER,
        kind=MessageKind.PLAIN_TEXT,
        sequence=sequence,
        text="上个月我的产量",
        payload={},
        created_at=created_at,
    )


async def test_commit_upserts_an_interaction_instead_of_duplicating_it(
    store: SqlInteractionStore,
) -> None:
    await store.commit(InteractionCommit(interaction=interaction("i-1")))
    await store.commit(
        InteractionCommit(
            interaction=interaction(
                "i-1", status=InteractionStatus.COMPLETED, last_event_sequence=3
            )
        )
    )

    stored = await store.get_interaction(OWNER, InteractionId("i-1"))

    assert stored is not None
    assert stored.status is InteractionStatus.COMPLETED
    assert stored.last_event_sequence == 3

    page = await store.list_interactions(OWNER, SESSION, limit=10)
    assert len(page.items) == 1


async def test_commit_writes_messages_and_events_in_one_transaction(
    engine: AsyncEngine,
    store: SqlInteractionStore,
) -> None:
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1", last_event_sequence=2),
            messages=(message("m-1", "i-1", 1),),
            events=(
                SessionEvent(sequence=1, name="interaction.started", data={}),
                SessionEvent(
                    sequence=2, name="interaction.completed", data={"status": "completed"}
                ),
            ),
            usage_events=(
                UsageOutboxEvent(
                    event_id="11111111-1111-4111-8111-111111111111",
                    event_type="interaction_started",
                    tenant_id=TENANT,
                    payload={"event_type": "interaction_started"},
                    created_at=NOW,
                ),
            ),
        )
    )

    events = await store.list_events(OWNER, InteractionId("i-1"), after_sequence=0)
    messages = await store.list_messages(OWNER, SESSION, limit=10)
    backlog = await SqlUsageOutbox(engine).backlog_size(NOW)

    assert [event.name for event in events] == [
        "interaction.started",
        "interaction.completed",
    ]
    assert len(messages.items) == 1
    assert backlog == 1


async def test_last_event_id_resume_returns_only_newer_events(store: SqlInteractionStore) -> None:
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1", last_event_sequence=3),
            events=tuple(
                SessionEvent(sequence=index, name=f"event.{index}", data={}) for index in (1, 2, 3)
            ),
        )
    )

    resumed = await store.list_events(OWNER, InteractionId("i-1"), after_sequence=2)

    assert [event.sequence for event in resumed] == [3]


async def test_another_user_cannot_read_an_interaction(store: SqlInteractionStore) -> None:
    await store.commit(InteractionCommit(interaction=interaction("i-1")))

    assert await store.get_interaction(INTRUDER, InteractionId("i-1")) is None
    assert await store.get_interaction(OTHER_TENANT, InteractionId("i-1")) is None
    assert await store.list_events(INTRUDER, InteractionId("i-1"), after_sequence=0) == ()


async def test_another_user_cannot_claim_or_delete(store: SqlInteractionStore) -> None:
    await store.commit(InteractionCommit(interaction=interaction("i-1")))

    assert await store.claim_run(INTRUDER, InteractionId("i-1"), NOW) is None
    assert await store.delete_session(INTRUDER, SESSION) is False
    assert await store.get_interaction(OWNER, InteractionId("i-1")) is not None


async def test_claim_run_is_won_by_exactly_one_caller(store: SqlInteractionStore) -> None:
    await store.commit(InteractionCommit(interaction=interaction("i-1")))

    first = await store.claim_run(OWNER, InteractionId("i-1"), NOW)
    second = await store.claim_run(OWNER, InteractionId("i-1"), NOW)

    assert first is not None
    assert first.status is InteractionStatus.RUNNING
    assert second is None


async def test_cursor_pagination_walks_every_message_exactly_once(
    store: SqlInteractionStore,
) -> None:
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1"),
            messages=tuple(
                message(f"m-{index}", "i-1", index, created_at=NOW + timedelta(seconds=index))
                for index in range(1, 8)
            ),
        )
    )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = await store.list_messages(OWNER, SESSION, limit=3, cursor=cursor)
        seen.extend(str(item.message_id) for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert seen == [f"m-{index}" for index in range(1, 8)]


async def test_deleting_a_session_cascades_to_messages_and_events(
    store: SqlInteractionStore,
) -> None:
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1", last_event_sequence=1),
            messages=(message("m-1", "i-1", 1),),
            events=(SessionEvent(sequence=1, name="interaction.started", data={}),),
        )
    )

    deleted = await store.delete_session(OWNER, SESSION)

    assert deleted is True
    assert await store.get_interaction(OWNER, InteractionId("i-1")) is None
    assert (await store.list_messages(OWNER, SESSION, limit=10)).items == ()
    assert await store.list_events(OWNER, InteractionId("i-1"), after_sequence=0) == ()


async def test_deleting_a_session_keeps_other_sessions(store: SqlInteractionStore) -> None:
    other = SessionId("s-2")
    await store.commit(InteractionCommit(interaction=interaction("i-1")))
    await store.commit(InteractionCommit(interaction=interaction("i-2", session_id=other)))

    await store.delete_session(OWNER, SESSION)

    assert await store.get_interaction(OWNER, InteractionId("i-2")) is not None


async def test_message_sequence_is_unique_within_an_interaction(
    store: SqlInteractionStore,
) -> None:
    await store.commit(
        InteractionCommit(interaction=interaction("i-1"), messages=(message("m-1", "i-1", 1),))
    )

    with pytest.raises(IntegrityError):
        await store.commit(
            InteractionCommit(interaction=interaction("i-1"), messages=(message("m-2", "i-1", 1),))
        )
