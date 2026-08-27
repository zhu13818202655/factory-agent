"""Outbox publisher behaviour: a usage-admin outage never changes an answer.

The publisher is a separate process, so these tests only assert its retry,
dead-letter, and backlog semantics; nothing here touches the session pipeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from factory_agent.ports import OutboxRecord
from factory_agent.usage.publisher import (
    MAX_BACKOFF_SECONDS,
    PublisherSettings,
    UsageOutboxPublisher,
    backoff_seconds,
)
from factory_agent.usage.sink import HttpUsageEventSink, UsagePublishError
from tests.support.session import FrozenClock, InMemoryUsageOutbox, RecordingUsageSink

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"


def record(event_id: str, attempts: int = 0, available_at: datetime = NOW) -> OutboxRecord:
    return OutboxRecord(
        event_id=event_id,
        event_type="llm_call_completed",
        tenant_id=TENANT,  # pyright: ignore[reportArgumentType]
        payload={"event_id": event_id, "event_type": "llm_call_completed"},
        attempts=attempts,
        available_at=available_at,
    )


def publisher(
    outbox: InMemoryUsageOutbox,
    sink: RecordingUsageSink,
    settings: PublisherSettings | None = None,
) -> UsageOutboxPublisher:
    return UsageOutboxPublisher(outbox, sink, FrozenClock(NOW), settings)


@pytest.mark.asyncio
async def test_a_successful_batch_marks_every_event_published() -> None:
    outbox = InMemoryUsageOutbox(records=[record("e-1"), record("e-2")])
    sink = RecordingUsageSink()

    cycle = await publisher(outbox, sink).run_once()

    assert cycle.published == 2
    assert cycle.backlog == 0
    assert sink.accepted == ["e-1", "e-2"]


@pytest.mark.asyncio
async def test_an_empty_outbox_does_not_call_the_sink() -> None:
    sink = RecordingUsageSink()

    cycle = await publisher(InMemoryUsageOutbox(), sink).run_once()

    assert cycle == type(cycle)(0, 0, 0, 0, 0)
    assert sink.accepted == []


@pytest.mark.asyncio
async def test_a_retryable_outage_reschedules_instead_of_dropping_events() -> None:
    outbox = InMemoryUsageOutbox(records=[record("e-1")])
    sink = RecordingUsageSink(failure=UsagePublishError("usage-admin timed out", retryable=True))

    cycle = await publisher(outbox, sink).run_once()

    assert (cycle.published, cycle.retried, cycle.dead_lettered) == (0, 1, 0)
    assert outbox.failed == [(("e-1",), "usage-admin timed out", False)]
    assert outbox.records[0].attempts == 1


@pytest.mark.asyncio
async def test_a_permanent_rejection_dead_letters_with_metadata() -> None:
    outbox = InMemoryUsageOutbox(records=[record("e-1")])
    sink = RecordingUsageSink(failure=UsagePublishError("schema rejected", retryable=False))

    cycle = await publisher(outbox, sink).run_once()

    assert (cycle.retried, cycle.dead_lettered) == (0, 1)
    assert outbox.failed == [(("e-1",), "schema rejected", True)]


@pytest.mark.asyncio
async def test_exhausted_attempts_are_dead_lettered_rather_than_retried_forever() -> None:
    outbox = InMemoryUsageOutbox(records=[record("e-1", attempts=7)])
    sink = RecordingUsageSink(failure=UsagePublishError("still down", retryable=True))

    cycle = await publisher(outbox, sink, PublisherSettings(max_attempts=8)).run_once()

    assert (cycle.retried, cycle.dead_lettered) == (0, 1)


@pytest.mark.asyncio
async def test_events_the_sink_did_not_accept_are_rescheduled() -> None:
    outbox = InMemoryUsageOutbox(records=[record("e-1"), record("e-2")])

    class PartialSink:
        async def publish(self, records: tuple[OutboxRecord, ...]) -> tuple[str, ...]:
            return (records[0].event_id,)

    cycle = await UsageOutboxPublisher(outbox, PartialSink(), FrozenClock(NOW)).run_once()

    assert (cycle.published, cycle.retried) == (1, 1)
    assert outbox.failed == [(("e-2",), "not_accepted", False)]


@pytest.mark.asyncio
async def test_a_batch_never_exceeds_the_configured_size() -> None:
    outbox = InMemoryUsageOutbox(records=[record(f"e-{index}") for index in range(10)])
    sink = RecordingUsageSink()

    cycle = await publisher(outbox, sink, PublisherSettings(batch_size=4)).run_once()

    assert cycle.claimed == 4
    assert cycle.backlog == 6


@pytest.mark.asyncio
async def test_events_scheduled_for_the_future_are_not_claimed() -> None:
    outbox = InMemoryUsageOutbox(records=[record("e-1", available_at=NOW + timedelta(minutes=5))])
    sink = RecordingUsageSink()

    cycle = await publisher(outbox, sink).run_once()

    assert (cycle.claimed, cycle.backlog) == (0, 0)


@pytest.mark.asyncio
async def test_backlog_after_outage_is_resent_when_usage_admin_recovers() -> None:
    """A usage-admin outage surfaces as backlog, then recovery resends."""
    outbox = InMemoryUsageOutbox(records=[record("e-1")])

    class AdvancingClock:
        def __init__(self, start: datetime) -> None:
            self._now = start

        def now(self) -> datetime:
            return self._now

        def advance(self, seconds: float) -> None:
            self._now += timedelta(seconds=seconds)

    clock = AdvancingClock(NOW)
    failing = UsageOutboxPublisher(
        outbox, RecordingUsageSink(failure=UsagePublishError("down", retryable=True)), clock
    )
    cycle = await failing.run_once()

    # The outage surfaces as a pending event scheduled for retry (the outbox
    # backlog alert metric tracks work that is due-now; this one is backoff).
    assert (cycle.published, cycle.retried) == (0, 1)
    assert outbox.failed == [(("e-1",), "down", False)]
    assert outbox.records[0].attempts == 1
    assert "e-1" not in outbox.published

    # After the retry delay elapses and usage-admin recovers, the same event is
    # published without any user-visible impact on the earlier answer.
    clock.advance(backoff_seconds(0))
    recovered = UsageOutboxPublisher(outbox, RecordingUsageSink(), clock)
    cycle = await recovered.run_once()

    assert cycle.published == 1
    assert cycle.backlog == 0
    assert outbox.published == ["e-1"]  # exactly one terminal publication


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(0, 1), (1, 2), (2, 4), (5, 32), (20, MAX_BACKOFF_SECONDS)],
)
def test_backoff_grows_exponentially_and_stays_bounded(attempts: int, expected: int) -> None:
    assert backoff_seconds(attempts) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_sink_treats_transient_http_status_as_retryable(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    sink = HttpUsageEventSink("http://usage-admin.invalid", transport=httpx.MockTransport(handler))
    async with sink:
        with pytest.raises(UsagePublishError) as caught:
            await sink.publish((record("e-1"),))

    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_sink_treats_a_schema_rejection_as_permanent() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "schema"})

    sink = HttpUsageEventSink("http://usage-admin.invalid", transport=httpx.MockTransport(handler))
    async with sink:
        with pytest.raises(UsagePublishError) as caught:
            await sink.publish((record("e-1"),))

    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_sink_posts_only_event_payloads_and_reports_accepted_ids() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read().decode())
        return httpx.Response(202, json={"accepted": ["e-1"]})

    sink = HttpUsageEventSink(
        "http://usage-admin.invalid", "token", transport=httpx.MockTransport(handler)
    )
    async with sink:
        accepted = await sink.publish((record("e-1"), record("e-2")))

    assert accepted == ("e-1",)
    assert '"events"' in str(seen[0])
