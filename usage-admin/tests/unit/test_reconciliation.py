"""Metering quantity conservation and reconciliation tests.

produced = accepted + duplicate + rejected must hold across deliveries, and
rolled_up counts must never exceed the accepted facts that reached rollup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from support.events import interaction_started, llm_call_completed
from usage_admin.ingest import IngestService
from usage_admin.rollup import RollupEngine
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)


def store() -> InMemoryUsageStore:
    return InMemoryUsageStore()


def ingest(active_store: InMemoryUsageStore) -> IngestService:
    return IngestService(active_store, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_quantity_conservation_across_deliveries() -> None:
    active_store = store()
    service = ingest(active_store)
    produced = 0
    accepted_total = duplicate_total = rejected_total = 0

    # First delivery: 3 events, all accepted.
    events = [interaction_started("e-1"), interaction_started("e-2"), llm_call_completed("e-3")]
    result = await service.ingest(events)
    produced += len(events)
    accepted_total += len(result.accepted)
    assert result.duplicate == () and result.rejected == ()

    # Redelivery of e-1 (same digest) -> duplicate; e-2 changed -> conflict.
    redelivery = [
        interaction_started("e-1"),
        interaction_started("e-2", capability="FR-005"),
    ]
    result = await service.ingest(redelivery)
    produced += len(redelivery)
    duplicate_total += len(result.duplicate)
    rejected_total += len(result.rejected)

    assert produced == accepted_total + duplicate_total + rejected_total
    assert duplicate_total == 1
    assert rejected_total == 1
    # e-1 and e-2 were accepted once; the redelivery added no raw rows.
    assert len(active_store.raw_events) == 3


@pytest.mark.asyncio
async def test_rolled_up_counts_never_exceed_accepted_facts() -> None:
    active_store = store()
    service = ingest(active_store)
    await service.ingest(
        [
            interaction_started("e-1"),
            interaction_started("e-1"),  # duplicate
            interaction_started("e-2"),
            interaction_started("e-3", capability="FR-005"),
        ]
    )
    await RollupEngine(active_store, clock=lambda: NOW).rollup_range(
        frozenset({"tenant-a"}), START, END
    )

    questions = [
        row.value
        for row in active_store.rollup_rows
        if row.metric == "questions" and row.granularity == "hour"
    ]
    rolled_up_questions = int(sum(questions))
    assert rolled_up_questions == 3  # e-1, e-2, e-3 started events
    assert rolled_up_questions <= len(active_store.interaction_facts)


@pytest.mark.asyncio
async def test_late_events_replay_into_rollup() -> None:
    active_store = store()
    service = ingest(active_store)
    late = interaction_started("late-1", occurred_at=(NOW - timedelta(hours=5)).isoformat())
    await service.ingest([late])
    await RollupEngine(active_store, clock=lambda: NOW).rollup_range(
        frozenset({"tenant-a"}), NOW - timedelta(hours=24), END
    )

    questions = [
        row.value
        for row in active_store.rollup_rows
        if row.metric == "questions" and row.granularity == "hour"
    ]
    assert int(sum(questions)) == 1


@pytest.mark.asyncio
async def test_replay_is_idempotent_without_duplicate_accumulation() -> None:
    active_store = store()
    service = ingest(active_store)
    await service.ingest([interaction_started("e-1")])
    await RollupEngine(active_store, clock=lambda: NOW).rollup_range(
        frozenset({"tenant-a"}), START, END
    )
    await RollupEngine(active_store, clock=lambda: NOW).rollup_range(
        frozenset({"tenant-a"}), START, END
    )

    questions = [
        row.value
        for row in active_store.rollup_rows
        if row.metric == "questions" and row.granularity == "hour"
    ]
    assert int(sum(questions)) == 1
