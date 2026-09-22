"""Session store behaviour against a real PostgreSQL database.

The offline proof that every business statement is ownership-filtered lives in
``tests/unit/persistence/test_session_queries.py``. This suite exercises the
same store against a real server so upsert semantics, cursor pagination,
``ON DELETE CASCADE`` and the ``PENDING -> RUNNING`` compare-and-set are proven
against the dialect that production uses.

Set ``FACTORY_AGENT_TEST_POSTGRES_URL`` to a disposable database to enable it.
The suite creates and drops its own schema and never touches customer data.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
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
from factory_agent.persistence import queries
from factory_agent.persistence.engine import normalize_dsn
from factory_agent.persistence.session_store import SqlInteractionStore
from factory_agent.persistence.tables import METADATA, usage_event_table
from factory_agent.ports import InteractionCommit, InteractionOwner, UsageEvent

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


async def test_informational_commit_never_resurrects_a_durable_terminal(
    store: SqlInteractionStore,
) -> None:
    """A progress commit advances bookkeeping but leaves the lifecycle alone.

    A live run's in-memory record still says ``running``; writing those columns
    over a terminal another process persisted would resurrect a cancelled run.
    """
    await store.commit(InteractionCommit(interaction=interaction("i-1", last_event_sequence=2)))
    await store.commit(
        InteractionCommit(
            interaction=interaction(
                "i-1",
                status=InteractionStatus.CANCELLED,
                state=SessionState.CANCELLED,
                last_event_sequence=3,
            )
        )
    )
    later = NOW + timedelta(seconds=30)

    await store.commit(
        InteractionCommit(
            interaction=replace(interaction("i-1", last_event_sequence=4), updated_at=later),
            events=(SessionEvent(sequence=4, name="interaction.progress", data={}),),
            lifecycle=False,
        )
    )

    stored = await store.get_interaction(OWNER, InteractionId("i-1"))
    assert stored is not None
    assert stored.status is InteractionStatus.CANCELLED
    assert stored.state is SessionState.CANCELLED
    assert stored.last_event_sequence == 4
    assert stored.updated_at == later
    events = await store.list_events(OWNER, InteractionId("i-1"), after_sequence=3)
    assert [event.name for event in events] == ["interaction.progress"]


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
                UsageEvent(
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
    async with engine.connect() as connection:
        stored_usage = (
            await connection.execute(sa.select(sa.func.count()).select_from(usage_event_table))
        ).scalar_one()

    assert [event.name for event in events] == [
        "interaction.started",
        "interaction.completed",
    ]
    assert len(messages.items) == 1
    assert stored_usage == 1


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


async def test_messages_with_identical_timestamps_keep_the_pipeline_order(
    store: SqlInteractionStore,
) -> None:
    """同一微秒落库的消息必须按 (interaction_id, sequence) 确定性排序。

    结果卡片和最终回答诞生于同一次 commit，created_at 可以完全相同；
    message_id 是随机 UUID，绝不允许参与排序。本用例特意让 message_id
    的字典序与 sequence 相反——排序键一旦退回 message_id，断言必然失败。
    """
    earlier = NOW - timedelta(seconds=60)
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-0", created_at=earlier, last_event_sequence=1),
            messages=(message("m-9", "i-0", 1, created_at=earlier),),
        )
    )
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-a", created_at=NOW, last_event_sequence=7),
            messages=(
                message("m-zzz", "i-a", 1, created_at=NOW),
                message("m-yyy", "i-a", 6, created_at=NOW),
                message("m-xxx", "i-a", 7, created_at=NOW),
            ),
        )
    )
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-b", created_at=NOW, last_event_sequence=2),
            messages=(
                message("m-www", "i-b", 1, created_at=NOW),
                message("m-vvv", "i-b", 2, created_at=NOW),
            ),
        )
    )

    page = await store.list_messages(OWNER, SESSION, limit=10)

    assert [(str(item.interaction_id), item.sequence) for item in page.items] == [
        ("i-0", 1),
        ("i-a", 1),
        ("i-a", 6),
        ("i-a", 7),
        ("i-b", 1),
        ("i-b", 2),
    ]


async def test_identical_timestamp_pages_walk_every_message_exactly_once(
    store: SqlInteractionStore,
) -> None:
    """三元组游标在整页同 created_at 数据上不重不漏."""
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-a", created_at=NOW, last_event_sequence=3),
            messages=tuple(
                message(f"m-{letter}", "i-a", index, created_at=NOW)
                for index, letter in enumerate(("c", "b", "a"), start=1)
            ),
        )
    )
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-b", created_at=NOW, last_event_sequence=2),
            messages=(
                message("m-z", "i-b", 1, created_at=NOW),
                message("m-y", "i-b", 2, created_at=NOW),
            ),
        )
    )

    seen: list[tuple[str, int]] = []
    cursor: str | None = None
    for _ in range(10):
        page = await store.list_messages(OWNER, SESSION, limit=2, cursor=cursor)
        seen.extend((str(item.interaction_id), item.sequence) for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert seen == [
        ("i-a", 1),
        ("i-a", 2),
        ("i-a", 3),
        ("i-b", 1),
        ("i-b", 2),
    ]


async def test_a_legacy_message_cursor_is_rejected_as_malformed(
    store: SqlInteractionStore,
) -> None:
    """旧版二元组游标在新排序语义下不可续页，显式报错让客户端从头重拉."""
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1"),
            messages=(message("m-1", "i-1", 1),),
        )
    )
    legacy = queries.encode_cursor(NOW, "m-1")

    with pytest.raises(queries.CursorError):
        await store.list_messages(OWNER, SESSION, limit=10, cursor=legacy)


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


async def test_startup_sweep_fails_every_stale_running_interaction_once(
    store: SqlInteractionStore,
) -> None:
    """The startup bulk compare-and-set repairs orphans exactly once."""
    stale_a = interaction(
        "i-stale-a",
        status=InteractionStatus.RUNNING,
        state=SessionState.EXECUTING,
        created_at=NOW - timedelta(seconds=700),
        last_event_sequence=2,
    )
    stale_b = interaction(
        "i-stale-b",
        status=InteractionStatus.RUNNING,
        state=SessionState.EXECUTING,
        created_at=NOW - timedelta(seconds=700),
        last_event_sequence=2,
    )
    fresh = interaction(
        "i-fresh",
        status=InteractionStatus.RUNNING,
        state=SessionState.EXECUTING,
        created_at=NOW - timedelta(seconds=10),
    )
    completed = interaction(
        "i-done",
        status=InteractionStatus.COMPLETED,
        state=SessionState.ANSWERED,
        created_at=NOW - timedelta(seconds=700),
    )
    for record in (stale_a, stale_b, fresh, completed):
        await store.commit(InteractionCommit(interaction=record))

    failed = await store.fail_stale_runs(
        stale_before=NOW - timedelta(seconds=600),
        now=NOW,
        category="executor_lost",
    )

    assert sorted(str(record.interaction_id) for record in failed) == ["i-stale-a", "i-stale-b"]
    for record in failed:
        assert record.status is InteractionStatus.FAILED
        assert record.error_category == "executor_lost"
        assert record.completed_at == NOW
        assert record.last_event_sequence == 3
    assert (await store.get_interaction(OWNER, InteractionId("i-fresh"))).status is (  # type: ignore[union-attr]
        InteractionStatus.RUNNING
    )
    assert (await store.get_interaction(OWNER, InteractionId("i-done"))).status is (  # type: ignore[union-attr]
        InteractionStatus.COMPLETED
    )

    # Idempotent: repeated and concurrent worker sweeps mark nothing.
    assert (
        await store.fail_stale_runs(
            stale_before=NOW - timedelta(seconds=600),
            now=NOW,
            category="executor_lost",
        )
        == ()
    )


async def test_abandoned_sweep_fails_only_unclaimed_pending_once(
    store: SqlInteractionStore,
) -> None:
    """A question no stream ever claimed is terminated; live rows are not."""
    unclaimed = interaction("i-unclaimed", created_at=NOW - timedelta(seconds=700))
    fresh = interaction("i-pending-fresh", created_at=NOW - timedelta(seconds=10))
    running = interaction(
        "i-running",
        status=InteractionStatus.RUNNING,
        state=SessionState.EXECUTING,
        created_at=NOW - timedelta(seconds=700),
    )
    completed = interaction(
        "i-done",
        status=InteractionStatus.COMPLETED,
        state=SessionState.ANSWERED,
        created_at=NOW - timedelta(seconds=700),
    )
    for record in (unclaimed, fresh, running, completed):
        await store.commit(InteractionCommit(interaction=record))

    failed = await store.fail_abandoned_runs(
        abandoned_before=NOW - timedelta(seconds=600),
        now=NOW,
        category="abandoned",
    )

    assert [str(record.interaction_id) for record in failed] == ["i-unclaimed"]
    reaped = failed[0]
    assert reaped.status is InteractionStatus.FAILED
    assert reaped.state is SessionState.FAILED
    assert reaped.error_category == "abandoned"
    assert reaped.completed_at == NOW
    # Terminal event sequence reserved for the caller to persist.
    assert reaped.last_event_sequence == 1
    assert (await store.get_interaction(OWNER, InteractionId("i-pending-fresh"))).status is (  # type: ignore[union-attr]
        InteractionStatus.PENDING
    )
    assert (await store.get_interaction(OWNER, InteractionId("i-running"))).status is (  # type: ignore[union-attr]
        InteractionStatus.RUNNING
    )
    assert (await store.get_interaction(OWNER, InteractionId("i-done"))).status is (  # type: ignore[union-attr]
        InteractionStatus.COMPLETED
    )

    # Idempotent: repeated and concurrent worker sweeps mark nothing.
    assert (
        await store.fail_abandoned_runs(
            abandoned_before=NOW - timedelta(seconds=600),
            now=NOW,
            category="abandoned",
        )
        == ()
    )


# --- Conversations ---------------------------------------------------------


def conversation_message(
    message_id: str,
    interaction_id: str,
    sequence: int,
    *,
    session_id: SessionId = SESSION,
    text: str = "上个月我的产量",
    role: MessageRole = MessageRole.USER,
    kind: MessageKind = MessageKind.PLAIN_TEXT,
    created_at: datetime = NOW,
    owner: InteractionOwner = OWNER,
) -> MessageRecord:
    """A message with a caller-chosen kind, unlike the shared ``message`` helper."""
    return MessageRecord(
        message_id=MessageId(message_id),
        interaction_id=InteractionId(interaction_id),
        session_id=session_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        role=role,
        kind=kind,
        sequence=sequence,
        text=text,
        payload={},
        created_at=created_at,
    )


async def test_the_first_commit_creates_the_conversation(store: SqlInteractionStore) -> None:
    """向后兼容：老前端不调建档接口，只提问，会话也必须出现在列表里."""
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1"),
            messages=(conversation_message("m-1", "i-1", 1),),
        )
    )

    page = await store.list_conversations(OWNER, limit=10)

    assert len(page.items) == 1
    summary = page.items[0]
    assert str(summary.conversation.session_id) == "s-1"
    assert summary.conversation.created_at == NOW
    assert summary.interaction_count == 1
    assert summary.message_count == 1
    assert summary.title_source == "上个月我的产量"
    assert summary.last_message is not None
    assert summary.last_message.kind is MessageKind.PLAIN_TEXT
    assert summary.last_status is InteractionStatus.PENDING


async def test_conversation_aggregates_skip_phase_and_pick_the_newest_rows(
    store: SqlInteractionStore,
) -> None:
    """一次列表水合要给出：非过程行计数、最新消息、最新轮次状态、最早问句."""
    later = NOW + timedelta(minutes=5)
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1", last_event_sequence=2),
            messages=(
                conversation_message("m-1", "i-1", 1, text="最早的问题"),
                conversation_message(
                    "m-2",
                    "i-1",
                    2,
                    role=MessageRole.ASSISTANT,
                    kind=MessageKind.PHASE,
                    text="正在取数",
                    created_at=NOW + timedelta(seconds=1),
                ),
                conversation_message(
                    "m-3",
                    "i-1",
                    3,
                    role=MessageRole.ASSISTANT,
                    kind=MessageKind.RESULT_TABLE,
                    text="已返回 1 行结果。",
                    created_at=NOW + timedelta(seconds=2),
                ),
            ),
        )
    )
    await store.commit(
        InteractionCommit(
            interaction=interaction(
                "i-2",
                status=InteractionStatus.COMPLETED,
                state=SessionState.ANSWERED,
                created_at=later,
            ),
            messages=(conversation_message("m-4", "i-2", 1, text="第二个问题", created_at=later),),
        )
    )

    summary = await store.get_conversation(OWNER, SESSION)

    assert summary is not None
    assert summary.interaction_count == 2
    # 过程行不计入，且不参与「最新消息」的竞争。
    assert summary.message_count == 3
    assert summary.last_message is not None
    assert str(summary.last_message.message_id) == "m-4"
    assert summary.last_status is InteractionStatus.COMPLETED
    assert summary.title_source == "最早的问题"
    assert summary.conversation.updated_at == later


async def test_conversation_recency_never_moves_backwards(store: SqlInteractionStore) -> None:
    """迟到的提交不能把会话从列表顶部挤下去（GREATEST 语义）."""
    newer = NOW + timedelta(minutes=10)
    await store.commit(InteractionCommit(interaction=interaction("i-1", created_at=newer)))
    await store.commit(InteractionCommit(interaction=interaction("i-2", created_at=NOW)))

    summary = await store.get_conversation(OWNER, SESSION)

    assert summary is not None
    assert summary.conversation.updated_at == newer


async def test_conversation_page_is_newest_first_and_never_repeats_a_row(
    store: SqlInteractionStore,
) -> None:
    """游标是「严格早于上一页最后一键」，所以不重不漏地走完整页集合."""
    for index, minutes in enumerate((30, 20, 10)):
        await store.commit(
            InteractionCommit(
                interaction=interaction(
                    f"i-{index}",
                    session_id=SessionId(f"s-{index}"),
                    created_at=NOW + timedelta(minutes=minutes),
                )
            )
        )

    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await store.list_conversations(OWNER, limit=2, cursor=cursor)
        seen.extend(str(summary.conversation.session_id) for summary in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == ["s-0", "s-1", "s-2"]


async def test_create_conversation_is_idempotent_and_keeps_messages(
    store: SqlInteractionStore,
) -> None:
    first = await store.create_conversation(OWNER, SESSION, NOW)
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1"),
            messages=(conversation_message("m-1", "i-1", 1),),
        )
    )
    again = await store.create_conversation(OWNER, SESSION, NOW + timedelta(minutes=1))

    assert first.created is True
    assert again.created is False
    assert again.summary.conversation.created_at == NOW
    assert again.summary.message_count == 1
    assert await store.count_conversations(OWNER) == 1


async def test_an_empty_conversation_is_listed_before_its_first_question(
    store: SqlInteractionStore,
) -> None:
    created = await store.create_conversation(OWNER, SessionId("s-empty"), NOW)

    page = await store.list_conversations(OWNER, limit=10)

    assert created.summary.interaction_count == 0
    assert created.summary.last_message is None
    assert created.summary.last_status is None
    assert created.summary.title_source is None
    assert [str(summary.conversation.session_id) for summary in page.items] == ["s-empty"]


async def test_conversations_are_invisible_to_other_owners(
    store: SqlInteractionStore,
) -> None:
    await store.commit(InteractionCommit(interaction=interaction("i-1")))

    assert (await store.list_conversations(INTRUDER, limit=10)).items == ()
    assert await store.get_conversation(INTRUDER, SESSION) is None
    assert await store.count_conversations(INTRUDER) == 0
    assert (await store.list_conversations(OTHER_TENANT, limit=10)).items == ()
    # 归属对也是主键：别人的会话与不存在的会话在详情上不可区分。
    assert await store.count_conversations(OWNER) == 1


async def test_concurrency_creates_exactly_one_conversation_row(
    store: SqlInteractionStore,
) -> None:
    """并发建档靠主键 + ON CONFLICT，而不是「先查后插」."""
    results = await asyncio.gather(
        *(store.create_conversation(OWNER, SessionId("s-race"), NOW) for _ in range(5))
    )

    assert sum(1 for result in results if result.created) == 1
    assert await store.count_conversations(OWNER) == 1


async def test_message_exclusion_is_pushed_into_sql(store: SqlInteractionStore) -> None:
    await store.commit(
        InteractionCommit(
            interaction=interaction("i-1"),
            messages=(
                conversation_message("m-1", "i-1", 1),
                conversation_message(
                    "m-2",
                    "i-1",
                    2,
                    role=MessageRole.ASSISTANT,
                    kind=MessageKind.PHASE,
                    text="正在取数",
                ),
            ),
        )
    )

    excluded = await store.list_messages(
        OWNER, SESSION, limit=10, exclude_kinds=frozenset({MessageKind.PHASE})
    )
    everything = await store.list_messages(OWNER, SESSION, limit=10)

    assert [str(record.message_id) for record in excluded.items] == ["m-1"]
    assert [str(record.message_id) for record in everything.items] == ["m-1", "m-2"]
