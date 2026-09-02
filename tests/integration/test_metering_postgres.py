"""Metering end-to-end against a real PostgreSQL (Story 11 6.2/6.3/6.4/6.6/6.7).

Set ``FACTORY_AGENT_TEST_POSTGRES_URL`` to a disposable database to enable the
suite. It creates and drops its own schema and never touches customer data.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from factory_agent.application.rollup import RollupEngine, hour_bucket
from factory_agent.domain import TenantId
from factory_agent.persistence.engine import create_migration_engine, normalize_dsn
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.persistence.rollup_store import SqlRollupStore
from factory_agent.persistence.tables import (
    METADATA,
    mes_call_fact_table,
    tenant_usage_hourly_table,
    usage_event_table,
)
from factory_agent.ports import UsageEvent

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


def usage_admin_alembic_config() -> Config:
    assert DATABASE_URL is not None
    config = Config()
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "usage-admin" / "migrations"))
    config.set_main_option("sqlalchemy.url", normalize_dsn(DATABASE_URL))
    return config


@pytest.fixture
def clean_database() -> Iterator[sa.Engine]:
    assert DATABASE_URL is not None
    engine = create_migration_engine(DATABASE_URL)

    def drop_everything() -> None:
        with engine.begin() as connection:
            METADATA.drop_all(connection)
            # usage-admin-owned tables are outside this service's METADATA.
            for table in (
                "tenant_registry",
                "admin_audit",
                "platform_principal",
                "usage_export",
            ):
                connection.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
            for table in ("alembic_version", "alembic_version_usage_admin"):
                connection.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
            # Stale partition helpers from an interrupted previous run would
            # block re-running the migrations as a different role (6.6).
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
    }
    return UsageEvent(
        event_id=event_id,
        event_type="mes_call_completed",
        tenant_id=TenantId(TENANT),
        payload=payload,
        created_at=occurred_at,
    )


async def test_direct_write_then_rollup_then_readable(engine: AsyncEngine) -> None:
    """6.2: one interaction with N MES calls -> usage_event + mes_call_fact ->
    idempotency -> rollup -> read via a usage-admin-style query."""
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

    # Rollup produces MES category metrics, then the usage-admin read path
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


async def test_migration_coexistence_both_orders(clean_database: sa.Engine) -> None:
    """6.6: factory-agent and usage-admin migrations succeed in either order."""
    # Order A: factory-agent first, then usage-admin.
    command.upgrade(alembic_config(), "head")
    command.upgrade(usage_admin_alembic_config(), "head")
    with clean_database.connect() as connection:
        version_rows = set(
            str(row[0])
            for row in connection.execute(
                sa.text(
                    "SELECT version_num FROM alembic_version"
                    " UNION ALL SELECT version_num FROM alembic_version_usage_admin"
                )
            )
        )
        names = set(
            str(row[0])
            for row in connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        )
    # Single development baseline per service (Story 11 5.4 history merge):
    # factory-agent = 20260824_0001_session, usage-admin = 20260827_0001_usage.
    assert "20260824_0001_session" in version_rows
    assert "20260827_0001_usage" in version_rows
    assert "usage_event" in names
    assert "tenant_registry" in names

    # Order B: usage-admin first, then factory-agent (fresh database).
    with clean_database.begin() as connection:
        METADATA.drop_all(connection)
        for table in (
            "tenant_registry",
            "admin_audit",
            "platform_principal",
            "usage_export",
            "alembic_version",
            "alembic_version_usage_admin",
        ):
            connection.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
    command.upgrade(usage_admin_alembic_config(), "head")
    command.upgrade(alembic_config(), "head")
    with clean_database.connect() as connection:
        usage_event_exists = connection.execute(
            sa.text("SELECT to_regclass('public.usage_event')")
        ).scalar_one()
        tenant_registry_exists = connection.execute(
            sa.text("SELECT to_regclass('public.tenant_registry')")
        ).scalar_one()
    assert usage_event_exists is not None
    assert tenant_registry_exists is not None


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
