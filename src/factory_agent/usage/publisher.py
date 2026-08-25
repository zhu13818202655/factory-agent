"""Independent usage outbox publisher.

Runs as its own process so a usage-admin outage never delays or changes an
interaction outcome. Retries use bounded exponential backoff; an event that
exhausts its attempts is dead-lettered with metadata rather than dropped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports import Clock, OutboxRecord, UsageEventSink, UsageOutbox
from factory_agent.usage.sink import UsagePublishError

_LOGGER = get_logger("usage.publisher")

MAX_BACKOFF_SECONDS = 600


@dataclass(frozen=True, slots=True)
class PublisherSettings:
    batch_size: int = 100
    poll_seconds: float = 5.0
    max_attempts: int = 8


@dataclass(frozen=True, slots=True)
class PublishCycle:
    """Outcome of one poll; ``backlog`` is the operational metric to alert on."""

    claimed: int
    published: int
    retried: int
    dead_lettered: int
    backlog: int


def backoff_seconds(attempts: int) -> int:
    return min(MAX_BACKOFF_SECONDS, 2 ** max(0, attempts))


class UsageOutboxPublisher:
    def __init__(
        self,
        outbox: UsageOutbox,
        sink: UsageEventSink,
        clock: Clock,
        settings: PublisherSettings | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._outbox = outbox
        self._sink = sink
        self._clock = clock
        self._settings = settings or PublisherSettings()
        self._sleep = sleep or asyncio.sleep

    async def run_once(self) -> PublishCycle:
        now = self._clock.now()
        records = await self._outbox.claim(self._settings.batch_size, now)
        if not records:
            return PublishCycle(0, 0, 0, 0, await self._outbox.backlog_size(now))

        try:
            accepted = await self._sink.publish(records)
        except UsagePublishError as exc:
            retried, dead = await self._reschedule(records, exc.reason, now, not exc.retryable)
            return PublishCycle(
                claimed=len(records),
                published=0,
                retried=retried,
                dead_lettered=dead,
                backlog=await self._outbox.backlog_size(now),
            )

        accepted_ids = tuple(dict.fromkeys(accepted))
        await self._outbox.mark_published(accepted_ids, now)
        remaining = tuple(record for record in records if record.event_id not in set(accepted_ids))
        retried, dead = await self._reschedule(remaining, "not_accepted", now, False)
        return PublishCycle(
            claimed=len(records),
            published=len(accepted_ids),
            retried=retried,
            dead_lettered=dead,
            backlog=await self._outbox.backlog_size(now),
        )

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            cycle = await self.run_once()
            _LOGGER.info(
                "usage.outbox.cycle",
                claimed=cycle.claimed,
                published=cycle.published,
                retried=cycle.retried,
                dead_lettered=cycle.dead_lettered,
                backlog=cycle.backlog,
            )
            await self._sleep(self._settings.poll_seconds)

    async def _reschedule(
        self,
        records: tuple[OutboxRecord, ...],
        reason: str,
        now: datetime,
        force_dead_letter: bool,
    ) -> tuple[int, int]:
        if not records:
            return 0, 0
        retry: list[OutboxRecord] = []
        dead: list[OutboxRecord] = []
        for record in records:
            exhausted = record.attempts + 1 >= self._settings.max_attempts
            (dead if force_dead_letter or exhausted else retry).append(record)

        if retry:
            await self._outbox.mark_failed(
                tuple(record.event_id for record in retry),
                reason,
                now + timedelta(seconds=backoff_seconds(retry[0].attempts)),
                False,
            )
        if dead:
            await self._outbox.mark_failed(
                tuple(record.event_id for record in dead), reason, now, True
            )
        return len(retry), len(dead)


__all__ = [
    "MAX_BACKOFF_SECONDS",
    "PublishCycle",
    "PublisherSettings",
    "UsageOutboxPublisher",
    "backoff_seconds",
]
