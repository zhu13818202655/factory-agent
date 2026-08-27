"""PostgreSQL usage store integration tests.

Requires ``USAGE_ADMIN_TEST_DATABASE_URL`` pointing at a disposable database
with the usage-admin migration applied; skipped otherwise. Covers the
idempotent ingest, granularity-correct rollup, and the ops summary against a
real partitioned event table (regression: hourly/daily rows must not leak
across tables).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from support.events import interaction_completed, interaction_started, llm_call_completed
from usage_admin.ingest import IngestService
from usage_admin.ops import OpsService
from usage_admin.platform import PlatformRole, PlatformScope
from usage_admin.rollup import RollupEngine
from usage_admin.store import PostgresUsageStore

DATABASE_URL = os.environ.get("USAGE_ADMIN_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="set USAGE_ADMIN_TEST_DATABASE_URL to a disposable database to run these tests",
)

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_ingest_rollup_summary_are_granularity_consistent() -> None:
    store = PostgresUsageStore(str(DATABASE_URL))
    ingest = IngestService(store, clock=lambda: NOW)
    await ingest.ingest(
        [
            interaction_started(
                "s-1", capability="FR-001", tenant_id="tenant-a", user_subject_id="u" * 64
            ),
            interaction_completed(
                "c-1",
                status="completed",
                duration_ms=1000,
                tenant_id="tenant-a",
                user_subject_id="u" * 64,
            ),
            llm_call_completed(
                "l-1", logical_call_id="call-1", prompt_tokens=120, tenant_id="tenant-a"
            ),
        ]
    )
    await RollupEngine(store, clock=lambda: NOW).rollup_range(frozenset({"tenant-a"}), START, END)

    scope = PlatformScope("ops-1", PlatformRole.ANALYST, frozenset())
    view = await OpsService(store, clock=lambda: NOW).summary(scope, START, END)

    # Hourly rollup must not double count the daily projection (regression).
    assert view.questions == 1
    assert view.users == 1
    assert view.tokens["prompt_tokens"] == 120
    assert view.durations["e2e_duration_ms"].p50_ms == 1000


@pytest.mark.asyncio
async def test_idempotent_ingest_is_deduped_in_postgres() -> None:
    store = PostgresUsageStore(str(DATABASE_URL))
    ingest = IngestService(store, clock=lambda: NOW)
    first = await ingest.ingest([interaction_started("dup-1", tenant_id="tenant-a")])
    second = await ingest.ingest([interaction_started("dup-1", tenant_id="tenant-a")])

    assert first.accepted == ("dup-1",)
    assert second.duplicate == ("dup-1",)
