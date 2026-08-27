"""Rollup engine tests over the in-memory store."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from support.events import (
    interaction_completed,
    interaction_started,
    llm_call_completed,
)
from usage_admin.ingest import IngestService
from usage_admin.rollup import RollupEngine
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


async def seed(events: list[dict[str, object]]) -> InMemoryUsageStore:
    store = InMemoryUsageStore()
    service = IngestService(store, clock=lambda: NOW)
    await service.ingest(events)
    return store


def engine(store: InMemoryUsageStore) -> RollupEngine:
    return RollupEngine(store, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_hourly_rollup_counts_questions_and_users() -> None:
    store = await seed(
        [
            interaction_started("s-1", user_subject_id="u" * 64),
            interaction_started("s-2", user_subject_id="u" * 64),
            interaction_started("s-3", user_subject_id="v" * 64),
        ]
    )

    run = await engine(store).rollup_range(frozenset({"tenant-a"}), START, END)

    assert run.hourly_rows >= 1
    rows = {
        row.metric: row.value
        for row in store.rollup_rows
        if row.bucket_start == NOW.replace(minute=0, second=0, microsecond=0)
        and row.granularity == "hour"
    }
    assert rows["questions"] == 3
    assert rows["users"] == 2


@pytest.mark.asyncio
async def test_daily_rollup_records_dau() -> None:
    store = await seed(
        [
            interaction_started("s-1", user_subject_id="u" * 64),
            interaction_started("s-2", user_subject_id="v" * 64),
        ]
    )

    await engine(store).rollup_range(frozenset({"tenant-a"}), START, END)

    daily = [row for row in store.rollup_rows if row.granularity == "day" and row.metric == "users"]
    assert any(row.value == 2 for row in daily)


@pytest.mark.asyncio
async def test_rollup_tracks_status_distribution_and_tokens() -> None:
    store = await seed(
        [
            interaction_completed("c-1", status="completed", duration_ms=1000),
            interaction_completed("c-2", status="failed", duration_ms=500),
            llm_call_completed("l-1", prompt_tokens=120, completion_tokens=40),
        ]
    )

    await engine(store).rollup_range(frozenset({"tenant-a"}), START, END)

    metrics = {
        row.metric: row.value
        for row in store.rollup_rows
        if row.granularity == "hour" and row.bucket_start.hour == 6
    }
    assert metrics["status.completed"] == 1
    assert metrics["status.failed"] == 1
    assert metrics["prompt_tokens"] == 120
    assert metrics["completion_tokens"] == 40
    assert metrics["e2e_duration_ms"] == 1500


@pytest.mark.asyncio
async def test_rollup_is_replayable_and_idempotent() -> None:
    store = await seed([interaction_started("s-1")])

    await engine(store).rollup_range(frozenset({"tenant-a"}), START, END)
    first = [row for row in store.rollup_rows]
    await engine(store).rollup_range(frozenset({"tenant-a"}), START, END)

    assert len([row for row in store.rollup_rows]) == len(first)
    assert all(row.rollup_version == "rollup-v1" for row in store.rollup_rows)


@pytest.mark.asyncio
async def test_empty_or_invalid_windows_rollup_to_nothing() -> None:
    store = InMemoryUsageStore()
    run = await engine(store).rollup_range(frozenset(), START, END)
    assert run.hourly_rows == 0 and run.daily_rows == 0

    run = await engine(store).rollup_range(frozenset({"tenant-a"}), END, START)
    assert run.hourly_rows == 0 and run.daily_rows == 0
