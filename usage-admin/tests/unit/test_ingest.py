"""Idempotent ingest policy tests against the in-memory store."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from support.events import interaction_started, llm_call_completed
from usage_admin.alerts import CollectingAlertSink
from usage_admin.events import canonical_digest
from usage_admin.ingest import IngestLimits, IngestService
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


def make_service(
    store: InMemoryUsageStore | None = None,
    *,
    limits: IngestLimits | None = None,
) -> tuple[IngestService, InMemoryUsageStore, CollectingAlertSink]:
    active_store = store or InMemoryUsageStore()
    alerts = CollectingAlertSink()
    service = IngestService(active_store, clock=lambda: NOW, limits=limits, alerts=alerts)
    return service, active_store, alerts


@pytest.mark.asyncio
async def test_first_delivery_is_accepted_and_stored() -> None:
    service, store, _ = make_service()
    event = interaction_started("e-1")

    result = await service.ingest([event])

    assert result.accepted == ("e-1",)
    assert result.duplicate == ()
    assert result.rejected == ()
    assert len(store.raw_events) == 1
    assert store.receipts["e-1"].payload_digest == canonical_digest(event)


@pytest.mark.asyncio
async def test_redelivery_with_same_digest_is_an_idempotent_duplicate() -> None:
    service, store, _ = make_service()
    event = interaction_started("e-1")
    await service.ingest([event])

    result = await service.ingest([dict(event)])

    assert result.duplicate == ("e-1",)
    assert len(store.raw_events) == 1
    assert len(store.interaction_facts) == 1


@pytest.mark.asyncio
async def test_same_event_id_with_different_digest_is_rejected_and_alerted() -> None:
    service, store, alerts = make_service()
    await service.ingest([interaction_started("e-1")])

    result = await service.ingest([interaction_started("e-1", capability="FR-005")])

    assert result.rejected == ("e-1",)
    assert result.reasons["e-1"] == "digest conflict"
    assert len(store.raw_events) == 1
    assert any(alert.kind == "ingest_digest_conflict" for alert in alerts.records)
    assert len(store.dead_letters) == 1


@pytest.mark.asyncio
async def test_schema_unsupported_event_goes_to_restricted_dead_letter() -> None:
    service, store, _ = make_service()
    event = interaction_started("e-1")
    event["secret_prompt"] = "canary-prompt"  # unknown field -> whitelist reject

    result = await service.ingest([event])

    assert result.rejected == ("e-1",)
    assert len(store.dead_letters) == 1
    entry = store.dead_letters[0]
    assert entry.event_id == "e-1"
    assert entry.reason == "unknown fields ['secret_prompt']"
    assert entry.tenant_id == "tenant-a"
    # The dead letter never stores the raw payload, only metadata.
    assert not hasattr(entry, "payload")


@pytest.mark.asyncio
async def test_dead_letter_never_contains_sensitive_payload() -> None:
    service, store, _ = make_service()
    event = interaction_started("e-1")
    event["employee_id"] = "E-CANARY"
    await service.ingest([event])

    serialized = str(store.dead_letters)
    assert "E-CANARY" not in serialized
    assert "canary" not in serialized.lower()


@pytest.mark.asyncio
async def test_over_count_batch_is_rejected_as_a_whole() -> None:
    service, store, _ = make_service(limits=IngestLimits(max_events=2))
    events = [interaction_started(f"e-{index}") for index in range(3)]

    result = await service.ingest(events)

    assert result.batch_reason == "batch exceeds 2 events"
    assert result.total == 0
    assert store.raw_events == []


@pytest.mark.asyncio
async def test_over_byte_batch_is_rejected_as_a_whole() -> None:
    service, store, _ = make_service(limits=IngestLimits(max_bytes=100))
    event = interaction_started("e-1", tenant_id="tenant-with-a-very-long-name" * 10)

    result = await service.ingest([event])

    assert result.batch_reason is not None
    assert result.total == 0
    assert store.raw_events == []


@pytest.mark.asyncio
async def test_empty_batch_is_a_noop() -> None:
    service, store, _ = make_service()

    result = await service.ingest([])

    assert result.total == 0
    assert store.raw_events == []


@pytest.mark.asyncio
async def test_llm_events_are_stored_as_facts() -> None:
    service, store, _ = make_service()
    await service.ingest([llm_call_completed("e-1")])

    assert len(store.llm_call_facts) == 1
    assert store.llm_call_facts[0].prompt_tokens == 120
