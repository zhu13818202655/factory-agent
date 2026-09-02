"""PostgreSQL usage store integration tests.

Requires ``USAGE_ADMIN_TEST_DATABASE_URL`` pointing at a disposable database
with the usage-admin and factory-agent (Story 11, single baseline
``20260824_0001_session``) migrations
applied; skipped otherwise. factory-agent writes the metering tables in a
separate transaction after its business commit, so this suite seeds
``interaction_fact`` /
``llm_call_fact`` / ``tenant_usage_hourly`` / ``tenant_usage_daily`` directly
with SQL (mirroring what factory-agent persists) and asserts the read-only
queries the ops layer relies on return correct aggregates — including the
regression that hourly rollup rows never leak across to the daily projection.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from support.events import interaction_completed, interaction_started, llm_call_completed
from usage_admin.events import InteractionFact, LlmCallFact
from usage_admin.ops import OpsService
from usage_admin.platform import PlatformRole, PlatformScope
from usage_admin.store import PostgresUsageStore

from factory_agent.persistence.engine import normalize_dsn

DATABASE_URL = os.environ.get("USAGE_ADMIN_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="set USAGE_ADMIN_TEST_DATABASE_URL to a disposable database to run these tests",
)


@pytest.fixture(scope="module", autouse=True)
def _applied_migrations() -> None:  # pyright: ignore[reportUnusedFunction]
    """Apply usage-admin + factory-agent (0004_metering) migrations so the
    metering tables exist; the suite then seeds them with SQL."""
    assert DATABASE_URL is not None
    dsn = normalize_dsn(DATABASE_URL)
    repository_root = Path(__file__).resolve().parents[3]
    for script_location in ("usage-admin/migrations", "migrations"):
        config = Config()
        config.set_main_option("script_location", str(repository_root / script_location))
        config.set_main_option("sqlalchemy.url", dsn)
        command.upgrade(config, "head")


NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"
USER = "u" * 64
ROLLUP_VERSION = "rollup-v2"


async def _insert_interaction_fact(
    connection: psycopg.AsyncConnection[object], fact: InteractionFact
) -> None:
    await connection.execute(
        """
        INSERT INTO interaction_fact (
            event_id, tenant_id, session_id, interaction_id, event_type,
            user_subject_id, occurred_at, capability_id, entrypoint, role_category,
            status, duration_ms, mes_duration_ms, llm_duration_ms, local_duration_ms,
            result_rows_bucket, error_category, received_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fact.event_id,
            fact.tenant_id,
            fact.session_id,
            fact.interaction_id,
            fact.event_type,
            fact.user_subject_id,
            fact.occurred_at,
            fact.capability_id,
            fact.entrypoint,
            fact.role_category,
            fact.status,
            fact.duration_ms,
            fact.mes_duration_ms,
            fact.llm_duration_ms,
            fact.local_duration_ms,
            fact.result_rows_bucket,
            fact.error_category,
            fact.received_at,
        ),
    )


async def _insert_llm_call_fact(
    connection: psycopg.AsyncConnection[object], fact: LlmCallFact
) -> None:
    await connection.execute(
        """
        INSERT INTO llm_call_fact (
            event_id, tenant_id, session_id, interaction_id, occurred_at,
            logical_call_id, stage, model_alias, actual_model, attempt,
            prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
            duration_ms, status, fallback_reason, error_category, received_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fact.event_id,
            fact.tenant_id,
            fact.session_id,
            fact.interaction_id,
            fact.occurred_at,
            fact.logical_call_id,
            fact.stage,
            fact.model_alias,
            fact.actual_model,
            fact.attempt,
            fact.prompt_tokens,
            fact.completion_tokens,
            fact.cached_tokens,
            fact.reasoning_tokens,
            fact.duration_ms,
            fact.status,
            fact.fallback_reason,
            fact.error_category,
            fact.received_at,
        ),
    )


async def _seed_metering_rows(database_url: str) -> None:
    """Write the metering rows factory-agent would have persisted (Story 11)."""
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        # Wipe any rows left over from a previous run, then seed fresh ones.
        tables = ("tenant_usage_daily", "tenant_usage_hourly", "interaction_fact", "llm_call_fact")
        for table in tables:
            await connection.execute(
                f"DELETE FROM {table} WHERE tenant_id = %s",
                (TENANT,),  # nosec B608 - fixed test table names
            )

        await _insert_interaction_fact(
            connection,
            interaction_started("s-1", capability="FR-001", tenant_id=TENANT, user_subject_id=USER),
        )
        await _insert_interaction_fact(
            connection,
            interaction_completed("c-1", duration_ms=1000, tenant_id=TENANT, user_subject_id=USER),
        )
        await _insert_llm_call_fact(
            connection,
            llm_call_completed(
                "l-1", logical_call_id="call-1", prompt_tokens=120, tenant_id=TENANT
            ),
        )

        for metric, value in (
            ("questions", 1.0),
            ("valid_questions", 1.0),
            ("status.completed", 1.0),
            ("llm_physical_attempts", 1.0),
            ("prompt_tokens", 120.0),
            ("e2e_duration_ms", 1000.0),
            ("e2e_duration_ms.count", 1.0),
        ):
            await connection.execute(
                """
                INSERT INTO tenant_usage_hourly
                    (tenant_id, bucket_start, metric, value, rollup_version, rolled_up_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (TENANT, START, metric, value, ROLLUP_VERSION, NOW),
            )
        # A daily row must never bleed into the hourly read (regression).
        await connection.execute(
            """
            INSERT INTO tenant_usage_daily
                (tenant_id, bucket_date, metric, value, rollup_version, rolled_up_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (TENANT, START.date(), "questions", 100.0, ROLLUP_VERSION, NOW),
        )


@pytest.mark.asyncio
async def test_summary_reads_seeded_metering_rows_without_cross_table_leak() -> None:
    await _seed_metering_rows(str(DATABASE_URL))
    store = PostgresUsageStore(str(DATABASE_URL))

    scope = PlatformScope("ops-1", PlatformRole.ANALYST, frozenset())
    view = await OpsService(store, clock=lambda: NOW).summary(scope, START, END)

    # Rollup rows come from tenant_usage_hourly (the daily row must not leak in).
    assert view.questions == 1
    assert view.valid_questions == 1
    assert view.status.get("status.completed") == 1
    assert view.tokens["prompt_tokens"] == 120
    # Distinct counts and percentiles come from the fact tables.
    assert view.users == 1
    assert view.llm_logical_calls == 1
    assert view.durations["e2e_duration_ms"].p50_ms == 1000
    assert view.freshness == NOW
