"""Standalone outbox publisher process (Story 8 wiring).

Runs the reviewed ``UsageOutboxPublisher`` over the PostgreSQL outbox and the
batch HTTP sink to usage-admin. A usage-admin outage never delays or changes an
answer outcome; this process retries with bounded backoff and dead-letters with
metadata after exhausting attempts.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Sequence
from datetime import datetime, timezone

from factory_agent.config import get_settings
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.session_store import SqlUsageOutbox
from factory_agent.usage.publisher import (
    PublisherSettings,
    UsageOutboxPublisher,
)
from factory_agent.usage.sink import HttpUsageEventSink


class _SystemClock:
    """Minimal wall clock; avoids a ``usage`` -> ``bootstrap`` dependency."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _usage_admin_api_key() -> str | None:
    settings = get_settings()
    if settings.usage_admin_api_key is None:
        return None
    return settings.usage_admin_api_key.get_secret_value()


async def run_forever() -> None:
    settings = get_settings()
    if settings.postgres_url is None:
        raise SystemExit("FACTORY_AGENT_POSTGRES_URL is required for the outbox publisher")
    if settings.usage_admin_base_url is None:
        raise SystemExit("FACTORY_AGENT_USAGE_ADMIN_BASE_URL is required for the outbox publisher")

    engine = create_session_engine(str(settings.postgres_url))
    outbox = SqlUsageOutbox(engine)
    sink = HttpUsageEventSink(
        str(settings.usage_admin_base_url),
        _usage_admin_api_key(),
        timeout_seconds=10.0,
    )
    publisher = UsageOutboxPublisher(
        outbox,
        sink,
        _SystemClock(),
        PublisherSettings(
            batch_size=settings.usage_outbox_batch_size,
            poll_seconds=settings.usage_outbox_poll_seconds,
            max_attempts=settings.usage_outbox_max_attempts,
        ),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await publisher.run_forever(stop)


def main(argv: Sequence[str] | None = None) -> None:
    del argv
    asyncio.run(run_forever())


__all__ = ["main", "run_forever"]
