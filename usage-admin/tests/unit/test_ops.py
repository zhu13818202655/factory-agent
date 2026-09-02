"""Platform ops service tests over the in-memory store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from support.events import (
    interaction_completed,
    interaction_started,
    llm_call_completed,
)
from usage_admin.ops import OpsQueryError, OpsService
from usage_admin.platform import PlatformRole, PlatformScope
from usage_admin.store import InMemoryUsageStore, RollupRow

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
# The query window starts at the clock's own hour so the rollup-coverage check
# never treats pre-window hours (which legitimately have no events) as gaps.
START = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

VIEWER = PlatformScope("ops-1", PlatformRole.VIEWER, frozenset())
SCOPED = PlatformScope("ops-2", PlatformRole.VIEWER, frozenset({"tenant-a"}))


async def build_store() -> InMemoryUsageStore:
    """Seed the facts and pre-aggregated rows factory-agent would have written.

    Rollup rows mirror ``factory_agent.application.rollup.compute_bucket_metrics``
    (rollup-v2); usage-admin only reads them.
    """
    store = InMemoryUsageStore()
    store.interaction_facts = [
        interaction_started(
            "s-1",
            capability="FR-001",
            tenant_id="tenant-a",
            user_subject_id="u" * 64,
        ),
        interaction_completed(
            "c-1",
            status="completed",
            duration_ms=1000,
            tenant_id="tenant-a",
            user_subject_id="u" * 64,
        ),
        interaction_started(
            "s-2",
            capability=None,
            tenant_id="tenant-b",
            user_subject_id="v" * 64,
        ),
    ]
    store.llm_call_facts = [
        llm_call_completed(
            "l-1",
            logical_call_id="call-1",
            prompt_tokens=120,
            tenant_id="tenant-a",
        ),
        llm_call_completed(
            "l-2",
            logical_call_id="call-1",
            attempt=2,
            prompt_tokens=120,
            tenant_id="tenant-a",
        ),
    ]
    store.rollup_rows = [
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="questions",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="valid_questions",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="status.completed",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="llm_physical_attempts",
            value=2,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="prompt_tokens",
            value=240,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="e2e_duration_ms",
            value=1000,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="e2e_duration_ms.count",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
        RollupRow(
            tenant_id="tenant-b",
            bucket_start=START,
            metric="questions",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        ),
    ]
    return store


def ops(store: InMemoryUsageStore) -> OpsService:
    return OpsService(store, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_summary_reports_metrics_with_versions_and_freshness() -> None:
    service = ops(await build_store())

    view = await service.summary(VIEWER, START, END)

    assert view.questions == 2
    assert view.valid_questions == 1
    assert view.users == 2
    assert view.llm_logical_calls == 1
    assert view.llm_physical_attempts == 2
    assert view.status["status.completed"] == 1
    assert view.tokens["prompt_tokens"] == 240  # l-1 + l-2 each contribute 120
    assert view.metric_version.startswith("rollup=")
    assert view.timezone == "Asia/Shanghai"
    assert view.freshness is not None
    assert view.incomplete is False


@pytest.mark.asyncio
async def test_scoped_principal_only_sees_its_tenants() -> None:
    service = ops(await build_store())

    view = await service.summary(SCOPED, START, END)

    assert view.questions == 1
    assert view.tenant_ids == ("tenant-a",)


@pytest.mark.asyncio
async def test_duration_percentiles_are_reported() -> None:
    service = ops(await build_store())

    view = await service.summary(VIEWER, START, END)

    stats = view.durations["e2e_duration_ms"]
    assert stats.count == 1
    assert stats.p50_ms == 1000
    assert stats.p95_ms == 1000
    assert stats.p99_ms == 1000


@pytest.mark.asyncio
async def test_timeseries_groups_by_granularity() -> None:
    service = ops(await build_store())

    view = await service.timeseries(VIEWER, START, END, "hour", ("questions", "users"))

    assert view.granularity == "hour"
    assert any(point.metrics["questions"] == 2 for point in view.points)


@pytest.mark.asyncio
async def test_dimensions_break_down_by_value() -> None:
    service = ops(await build_store())

    view = await service.dimensions(VIEWER, START, END, "capability")

    assert view.values["FR-001"] == 1
    assert "FR-999" not in view.values


@pytest.mark.asyncio
async def test_users_returns_pseudonyms_with_counts_and_pagination() -> None:
    service = ops(await build_store())

    page = await service.users(VIEWER, START, END, limit=1, offset=0)

    assert page.total == 2
    assert len(page.items) == 1
    assert page.next_cursor == 1
    assert all(item.user_subject_id in ("u" * 64, "v" * 64) for item in page.items)


@pytest.mark.asyncio
async def test_over_span_query_is_rejected_structured() -> None:
    service = ops(await build_store())
    wide_start = END - timedelta(days=400)

    with pytest.raises(OpsQueryError, match="span budget"):
        await service.summary(VIEWER, wide_start, END)


@pytest.mark.asyncio
async def test_inverted_range_is_rejected() -> None:
    service = ops(await build_store())

    with pytest.raises(OpsQueryError, match="start before end"):
        await service.summary(VIEWER, END, START)


@pytest.mark.asyncio
async def test_unsupported_dimension_is_rejected() -> None:
    service = ops(await build_store())

    with pytest.raises(OpsQueryError, match="unsupported dimension"):
        await service.dimensions(VIEWER, START, END, "sql")


@pytest.mark.asyncio
async def test_negative_offset_is_rejected() -> None:
    service = ops(await build_store())

    with pytest.raises(OpsQueryError, match="offset"):
        await service.users(VIEWER, START, END, limit=10, offset=-1)


@pytest.mark.asyncio
async def test_empty_scope_returns_empty_summary() -> None:
    service = ops(InMemoryUsageStore())
    view = await service.summary(VIEWER, START, END)
    assert view.questions == 0
    assert view.incomplete is False
