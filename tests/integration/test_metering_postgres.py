"""Metering end-to-end against a real PostgreSQL.

Set ``FACTORY_AGENT_TEST_POSTGRES_URL`` to a disposable database to enable the
suite. It creates and drops its own schema and never touches customer data.
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from factory_agent.application.rollup import RollupEngine, hour_bucket
from factory_agent.domain import TenantId
from factory_agent.persistence.engine import create_migration_engine, normalize_dsn
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.persistence.rollup_store import SqlRollupStore
from factory_agent.persistence.tables import (
    METADATA,
    llm_call_fact_table,
    mes_call_fact_table,
    tenant_usage_hourly_table,
    usage_event_table,
)
from factory_agent.ports import UsageEvent
from factory_agent.statistics.partitions import UsagePartitionMaintainer

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("FACTORY_AGENT_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set FACTORY_AGENT_TEST_POSTGRES_URL to a disposable database to run these tests",
    ),
    pytest.mark.asyncio,
]

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"


def async_url(url: str) -> str:
    return normalize_dsn(url)


def alembic_config() -> Config:
    assert DATABASE_URL is not None
    config = Config()
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", normalize_dsn(DATABASE_URL))
    return config


def current_head(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


@pytest.fixture
def clean_database() -> Iterator[sa.Engine]:
    assert DATABASE_URL is not None
    engine = create_migration_engine(DATABASE_URL)

    def drop_everything() -> None:
        with engine.begin() as connection:
            METADATA.drop_all(connection)
            # ``METADATA`` covers every table in the single baseline, platform
            # tables included; only the version table needs dropping by hand.
            for table in ("alembic_version",):
                connection.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
            # Stale partition helpers from an interrupted previous run would
            # block re-running the migrations as a different role.
            connection.execute(
                sa.text("DROP FUNCTION IF EXISTS factory_agent_create_partition(DATE)")
            )

    drop_everything()
    try:
        yield engine
    finally:
        drop_everything()
        engine.dispose()


@pytest_asyncio.fixture
async def engine(clean_database: sa.Engine) -> AsyncIterator[AsyncEngine]:
    assert DATABASE_URL is not None
    command.upgrade(alembic_config(), "head")
    # The baseline seeds by execution time, so the month this suite writes into
    # is only present when it happens to be the current one. Ask the maintainer
    # for it explicitly: a partitioned table has no default partition, and a
    # missing month is a hard insert failure rather than a silent one.
    maintainer = UsagePartitionMaintainer(str(DATABASE_URL), clock=lambda: NOW.date())
    await maintainer.ensure(NOW.date())
    created = create_async_engine(async_url(DATABASE_URL), poolclass=sa.pool.NullPool)
    try:
        yield created
    finally:
        await created.dispose()


def mes_event(
    event_id: str,
    operation_id: str = "YskQuery",
    *,
    status: str = "completed",
    occurred_at: datetime = NOW,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    parent_span_id: str | None = None,
) -> UsageEvent:
    payload: dict[str, object] = {
        "event_id": event_id,
        "schema_version": "1.0",
        "occurred_at": occurred_at.isoformat(),
        "tenant_id": TENANT,
        "user_subject_id": "u1",
        "session_id": "s-1",
        "interaction_id": "i-1",
        "trace_id": "0" * 32,
        "event_type": "mes_call_completed",
        "operation_id": operation_id,
        "page_count": 1,
        "row_count_bucket": "1-10",
        "duration_ms": 12,
        "status": status,
        "error_category": None if status == "completed" else "internal_error",
        # Optional by design: an adapter that has the two clock readings supplies
        # them, and one that does not must leave them NULL rather than have a
        # value invented for it (migration 20260922_0003). Left out entirely by
        # older callers, so the key is present-but-null here rather than absent.
        "started_at": started_at.isoformat() if started_at is not None else None,
        "ended_at": ended_at.isoformat() if ended_at is not None else None,
        "parent_span_id": parent_span_id,
    }
    return UsageEvent(
        event_id=event_id,
        event_type="mes_call_completed",
        tenant_id=TenantId(TENANT),
        payload=payload,
        created_at=occurred_at,
    )


def llm_event(
    event_id: str,
    logical_call_id: str = "lc_8f21",
    *,
    attempt: int = 1,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    parent_span_id: str | None = None,
) -> UsageEvent:
    payload: dict[str, object] = {
        "event_id": event_id,
        "schema_version": "1.0",
        "occurred_at": NOW.isoformat(),
        "tenant_id": TENANT,
        "user_subject_id": "u1",
        "session_id": "s-1",
        "interaction_id": "i-1",
        "trace_id": "0" * 32,
        "event_type": "llm_call_completed",
        "logical_call_id": logical_call_id,
        "stage": "extract",
        "model_alias": "factory-fast",
        "actual_model": "Qwen3-32B-Instruct",
        "attempt": attempt,
        "prompt_tokens": 1_842,
        "completion_tokens": 96,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "duration_ms": 1_180,
        "status": "completed",
        "fallback_reason": None,
        "error_category": None,
        "started_at": started_at.isoformat() if started_at is not None else None,
        "ended_at": ended_at.isoformat() if ended_at is not None else None,
        "parent_span_id": parent_span_id,
    }
    return UsageEvent(
        event_id=event_id,
        event_type="llm_call_completed",
        tenant_id=TenantId(TENANT),
        payload=payload,
        created_at=NOW,
    )


async def test_direct_write_then_rollup_then_readable(engine: AsyncEngine) -> None:
    """6.2: one interaction with N MES calls -> usage_event + mes_call_fact ->
    idempotency -> rollup -> read via the statistics surface's query."""
    store = SqlMeteringStore(engine)
    events = (
        mes_event("e-1", "YskQuery", status="completed", occurred_at=NOW),
        mes_event("e-2", "YskQuery", status="completed", occurred_at=NOW + timedelta(seconds=1)),
        mes_event("e-3", "GongziMxQuery", status="failed", occurred_at=NOW + timedelta(seconds=2)),
    )
    await store.write_usage_events(events)

    # Idempotency: replaying the same event_ids records nothing new.
    await store.write_usage_events(events)

    async with engine.connect() as connection:
        archived = (
            await connection.execute(sa.select(sa.func.count()).select_from(usage_event_table))
        ).scalar_one()
        facts = (
            await connection.execute(
                sa.select(mes_call_fact_table.c.operation_id, mes_call_fact_table.c.status)
            )
        ).all()
    assert archived == 3
    assert sorted((str(row[0]), str(row[1])) for row in facts) == [
        ("GongziMxQuery", "failed"),
        ("YskQuery", "completed"),
        ("YskQuery", "completed"),
    ]

    # Rollup produces MES category metrics, then the statistics read path
    # (a plain select on the rollup table) sees them.
    rollup_store = SqlRollupStore(engine)
    categories = await rollup_store.list_mes_categories()
    assert categories["YskQuery"] == "output"
    assert categories["GongziMxQuery"] == "payroll"

    engine_rollup = RollupEngine(rollup_store, clock=lambda: NOW + timedelta(hours=1))
    run = await engine_rollup.rollup_range(
        frozenset({TENANT}), NOW - timedelta(hours=1), NOW + timedelta(hours=2)
    )
    assert run.hourly_rows == 1
    assert run.daily_rows == 1

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(
                    tenant_usage_hourly_table.c.metric,
                    tenant_usage_hourly_table.c.value,
                ).where(tenant_usage_hourly_table.c.tenant_id == TENANT)
            )
        ).all()
    metrics = {str(row[0]): float(row[1]) for row in rows}
    assert metrics["mes_calls"] == 3
    assert metrics["mes_calls.completed"] == 2
    assert metrics["mes_calls.failed"] == 1
    assert metrics["mes_calls.output"] == 2
    assert metrics["mes_calls.payroll"] == 1
    assert metrics["mes_calls.order"] == 0


async def test_the_mes_timeline_edges_reach_the_fact_row(engine: AsyncEngine) -> None:
    """The timeline columns are written, and not invented when absent.

    Two rows with different provenance, because these are the two cases that can
    silently regress: an adapter that supplies the edges from its own two clock
    readings, and one that does not. The second must land ``NULL`` — migration
    ``20260922_0003`` left the columns nullable on purpose instead of
    backfilling, because a missing measurement must stay distinguishable from a
    real one, or the waterfall starts inventing bar positions.
    """
    started = NOW - timedelta(milliseconds=12)
    store = SqlMeteringStore(engine)
    await store.write_usage_events(
        (
            mes_event(
                "t-1",
                "SystemToken",
                started_at=started,
                ended_at=NOW,
                parent_span_id="ph_authorizing",
            ),
            mes_event("t-2", "YskQuery"),
        )
    )

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(
                    mes_call_fact_table.c.operation_id,
                    mes_call_fact_table.c.started_at,
                    mes_call_fact_table.c.ended_at,
                    mes_call_fact_table.c.parent_span_id,
                ).order_by(mes_call_fact_table.c.operation_id)
            )
        ).all()

    by_operation = {str(row[0]): row for row in rows}
    measured = by_operation["SystemToken"]
    assert measured[1] == started
    assert measured[2] == NOW
    assert measured[3] == "ph_authorizing"
    omitted = by_operation["YskQuery"]
    assert omitted[1] is None
    assert omitted[2] is None
    assert omitted[3] is None


async def test_the_llm_timeline_edges_and_parent_reach_the_fact_row(
    engine: AsyncEngine,
) -> None:
    """A retry group shares one parent: that is what ``parent_span_id`` buys.

    ``logical_call_id`` alone is flat — it can name the call but not the phase
    that triggered it, so "round 2 re-entered EXTRACT" was unrepresentable. Both
    rows below are attempts of one logical call under one phase span.
    """
    store = SqlMeteringStore(engine)
    await store.write_usage_events(
        (
            llm_event(
                "a-1",
                attempt=1,
                started_at=NOW,
                ended_at=NOW + timedelta(milliseconds=400),
                parent_span_id="ph_parsing",
            ),
            llm_event(
                "a-2",
                attempt=2,
                started_at=NOW + timedelta(milliseconds=500),
                ended_at=NOW + timedelta(milliseconds=1_180),
                parent_span_id="ph_parsing",
            ),
        )
    )

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(
                    llm_call_fact_table.c.attempt,
                    llm_call_fact_table.c.started_at,
                    llm_call_fact_table.c.ended_at,
                    llm_call_fact_table.c.parent_span_id,
                ).order_by(llm_call_fact_table.c.attempt)
            )
        ).all()

    assert [int(row[0]) for row in rows] == [1, 2]
    assert [row[3] for row in rows] == ["ph_parsing", "ph_parsing"]
    # Edges are per-attempt, not per logical call: the retry starts later.
    assert rows[1][1] > rows[0][1]
    assert rows[1][2] > rows[0][2]


async def test_failure_isolation_preserves_business_and_rollup(engine: AsyncEngine) -> None:
    """6.3: a metering fault alerts and never blocks; business data stays intact."""
    from factory_agent.domain import (
        InteractionId,
        InteractionRecord,
        InteractionStatus,
        MessageId,
        MessageKind,
        MessageRecord,
        MessageRole,
        SessionId,
        SessionState,
        UserId,
    )
    from factory_agent.persistence.session_store import SqlInteractionStore
    from factory_agent.ports import InteractionCommit, InteractionOwner

    metering = SqlMeteringStore(engine)
    alerted: list[Exception] = []
    metering._on_failure = alerted.append  # type: ignore[attr-defined]
    store = SqlInteractionStore(engine, metering=metering)

    # 1) A normal turn commits business data and metering in one flow.
    owner = InteractionOwner(tenant_id=TenantId(TENANT), user_id=UserId("user-a"))
    await store.commit(
        InteractionCommit(
            interaction=InteractionRecord(
                interaction_id=InteractionId("i-1"),
                session_id=SessionId("s-1"),
                tenant_id=TenantId(TENANT),
                user_id=UserId("user-a"),
                status=InteractionStatus.PENDING,
                state=SessionState.PARSING,
                input_text="上个月我的产量",
                capability_id=None,
                clarification_rounds=0,
                last_event_sequence=0,
                error_category=None,
                created_at=NOW,
                updated_at=NOW,
                completed_at=None,
            ),
            messages=(
                MessageRecord(
                    message_id=MessageId("m-1"),
                    interaction_id=InteractionId("i-1"),
                    session_id=SessionId("s-1"),
                    tenant_id=TenantId(TENANT),
                    user_id=UserId("user-a"),
                    role=MessageRole.USER,
                    kind=MessageKind.PLAIN_TEXT,
                    sequence=1,
                    text="上个月我的产量",
                    payload={},
                    created_at=NOW,
                ),
            ),
            usage_events=(mes_event("ok-1"),),
        )
    )

    # 2) Drop the metering table out from under the store: the next metering
    #    write must fail silently (alerted, not raised).
    async with engine.begin() as connection:
        await connection.execute(sa.text("DROP TABLE usage_event"))
        await connection.execute(sa.text("DROP TABLE mes_call_fact"))

    await store.commit(
        InteractionCommit(
            interaction=InteractionRecord(
                interaction_id=InteractionId("i-1"),
                session_id=SessionId("s-1"),
                tenant_id=TenantId(TENANT),
                user_id=UserId("user-a"),
                status=InteractionStatus.COMPLETED,
                state=SessionState.ANSWERED,
                input_text="上个月我的产量",
                capability_id=None,
                clarification_rounds=0,
                last_event_sequence=1,
                error_category=None,
                created_at=NOW,
                updated_at=NOW + timedelta(seconds=1),
                completed_at=NOW + timedelta(seconds=1),
            ),
            usage_events=(mes_event("lost-1"),),
        )
    )

    # 3) The business data is intact, the answer commit succeeded, and exactly
    #    one structured alert was recorded for the failed metering write.
    assert len(alerted) == 1
    record = await store.get_interaction(owner, InteractionId("i-1"))
    assert record is not None
    assert record.status is InteractionStatus.COMPLETED
    messages = await store.list_messages(owner, SessionId("s-1"), limit=10)
    assert len(messages.items) == 1


async def test_single_baseline_builds_metering_and_platform_tables(
    clean_database: sa.Engine,
) -> None:
    """One revision builds the whole database, platform tables included."""
    config = alembic_config()
    command.upgrade(config, "head")

    head = ScriptDirectory.from_config(config).get_current_head()
    with clean_database.connect() as connection:
        versions = {
            str(row[0])
            for row in connection.execute(sa.text("SELECT version_num FROM alembic_version"))
        }
        names = {
            str(row[0])
            for row in connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        }
    assert versions == {head}
    assert "usage_event" in names
    assert "tenant_registry" in names
    assert "admin_audit" in names
    assert "platform_principal" in names
    assert "usage_export" in names
    # The retired artifact table must not come back through a downgrade path.
    assert "agent_artifact" not in names


async def test_tenant_registry_seed_classification_matches_catalog(engine: AsyncEngine) -> None:
    """6.1/2.3 integration: the seeded mapping covers every catalog operation."""
    from factory_agent.persistence.rollup_store import mes_operation_category_table

    async with engine.connect() as connection:
        rows = (
            await connection.execute(sa.select(mes_operation_category_table.c.operation_id))
        ).all()
    seeded = {str(row[0]) for row in rows}

    import yaml

    document = yaml.safe_load(
        (REPOSITORY_ROOT / "configs" / "knowledge" / "apis.yaml").read_text(encoding="utf-8")
    )
    catalog_ids = {operation["operation_id"] for operation in document["operations"]}
    assert seeded == catalog_ids


async def test_mes_category_metrics_are_versioned(engine: AsyncEngine) -> None:
    """6.1/3.3: rollup rows carry the version and are idempotently replayable."""
    from factory_agent.application.rollup import ROLLUP_VERSION
    from factory_agent.persistence.rollup_store import RollupRow

    store = SqlMeteringStore(engine)
    await store.write_usage_events((mes_event("v-1"),))

    rollup_store = SqlRollupStore(engine)
    await rollup_store.upsert_rollup_rows(
        [
            RollupRow(
                tenant_id=TENANT,
                bucket_start=hour_bucket(NOW),
                metric="mes_calls",
                value=1.0,
                rollup_version=ROLLUP_VERSION,
                rolled_up_at=NOW,
                granularity="hour",
            )
        ]
    )
    # Replay with a different value: the upsert overwrites, never duplicates.
    await rollup_store.upsert_rollup_rows(
        [
            RollupRow(
                tenant_id=TENANT,
                bucket_start=hour_bucket(NOW),
                metric="mes_calls",
                value=2.0,
                rollup_version=ROLLUP_VERSION,
                rolled_up_at=NOW,
                granularity="hour",
            )
        ]
    )
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(tenant_usage_hourly_table.c.value).where(
                    tenant_usage_hourly_table.c.metric == "mes_calls"
                )
            )
        ).all()
    assert [float(row[0]) for row in rows] == [2.0]


# --- usage_event partition maintenance (D-9) --------------------------------


async def test_baseline_seeds_the_current_and_following_month(
    clean_database: sa.Engine,
) -> None:
    """The migration seeds by execution time, not by a hard-coded calendar.

    A written-in seed silently drops every write once the window is passed, and
    because metering failures are alerted rather than fatal, that shows up as
    missing billing rows and nothing else.
    """
    command.upgrade(alembic_config(), "head")

    with clean_database.connect() as connection:
        current = connection.execute(
            sa.text(
                "SELECT to_regclass("
                "'public.usage_event_' || to_char(date_trunc('month', now()), 'YYYYMM'))"
            )
        ).scalar_one()
        following = connection.execute(
            sa.text(
                "SELECT to_regclass("
                "'public.usage_event_' || to_char("
                "date_trunc('month', now()) + interval '1 month', 'YYYYMM'))"
            )
        ).scalar_one()

    assert current is not None
    assert following is not None


async def test_ensure_creates_the_requested_months_and_accepts_writes(
    clean_database: sa.Engine,
) -> None:
    """Acceptance: ``ensure(date(2026, 11, 1))`` -> 202611 / 202612 + a write."""

    command.upgrade(alembic_config(), "head")

    maintainer = UsagePartitionMaintainer(str(DATABASE_URL), clock=lambda: date(2026, 11, 1))
    run = await maintainer.ensure(date(2026, 11, 1))

    assert [target.name for target in run.failed] == []
    assert [target.name for target in run.ensured] == ["usage_event_202611", "usage_event_202612"]

    with clean_database.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO usage_event (event_id, schema_version, event_type, tenant_id,"
                " occurred_at, received_at, user_subject_id, session_id, interaction_id,"
                " trace_id, payload)"
                " VALUES ('p-1', '1.0', 'mes_call_completed', :tenant,"
                " TIMESTAMPTZ '2026-11-05 09:00:00+08', TIMESTAMPTZ '2026-11-05 09:00:01+08',"
                " 'u1', 's-1', 'i-1', :trace, '{}'::jsonb)"
            ),
            {"tenant": TENANT, "trace": "0" * 32},
        )
        routed = connection.execute(
            sa.text("SELECT tableoid::regclass::text FROM usage_event WHERE event_id = 'p-1'")
        ).scalar_one()

    assert routed == "usage_event_202611"


async def test_ensure_is_idempotent_and_repairs_a_dropped_partition(
    clean_database: sa.Engine,
) -> None:
    """A partition dropped by hand comes back on the next pass."""

    command.upgrade(alembic_config(), "head")
    maintainer = UsagePartitionMaintainer(str(DATABASE_URL), clock=lambda: date(2026, 11, 1))
    await maintainer.ensure(date(2026, 11, 1))

    # Re-running changes nothing (the helper is CREATE TABLE IF NOT EXISTS).
    again = await maintainer.ensure(date(2026, 11, 1))
    assert [target.name for target in again.failed] == []

    with clean_database.begin() as connection:
        connection.execute(sa.text("DROP TABLE usage_event_202611"))
    dropped = await maintainer.ensure(date(2026, 11, 1))

    assert [target.name for target in dropped.ensured] == [
        "usage_event_202611",
        "usage_event_202612",
    ]
    with clean_database.connect() as connection:
        repaired = connection.execute(
            sa.text("SELECT to_regclass('public.usage_event_202611')")
        ).scalar_one()
    assert repaired is not None


async def test_a_failed_maintenance_pass_reports_and_alerts(
    clean_database: sa.Engine,
) -> None:
    """A read-only connection cannot create the partition: report, never raise.

    This is the failure path the cross-month guarantee depends on. A maintenance
    failure must never reach the caller (the API lifespan) as an exception, but it
    must never be silent either: every failed month comes back in the run report
    and raises an alert carrying the exact repair month.
    """
    command.upgrade(alembic_config(), "head")
    with clean_database.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS usage_event_202611"))
        connection.execute(sa.text("DROP TABLE IF EXISTS usage_event_202612"))

    alerted: list[tuple[str, dict[str, object]]] = []

    class Alerts:
        async def alert(self, kind: str, detail: dict[str, object]) -> None:
            alerted.append((kind, detail))

    maintainer = UsagePartitionMaintainer(
        read_only_dsn(str(DATABASE_URL)),
        alerts=Alerts(),
        clock=lambda: date(2026, 11, 1),
    )

    run = await maintainer.ensure(date(2026, 11, 1))

    assert [target.name for target in run.ensured] == []
    assert [target.name for target in run.failed] == [
        "usage_event_202611",
        "usage_event_202612",
    ]
    assert [kind for kind, _ in alerted] == [
        "usage.partition.ensure_failed",
        "usage.partition.ensure_failed",
    ]
    for _, detail in alerted:
        assert detail["reason"] == "create_failed"
        assert detail["month"] in {"2026-11-01", "2026-12-01"}
    with clean_database.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT to_regclass('public.usage_event_202611')")
            ).scalar_one()
            is None
        )


def read_only_dsn(url: str) -> str:
    """The same DSN with a read-only default transaction.

    ``options`` is a libpq startup parameter, so the read-only mode is in force
    from the very first statement — issuing ``SET TRANSACTION READ ONLY`` first
    would leave the DDL free to run.
    """
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}options=-c%20default_transaction_read_only%3Don"
