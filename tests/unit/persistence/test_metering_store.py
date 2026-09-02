"""Metering store failure isolation.

``SqlMeteringStore.write_usage_events`` must never raise: any database fault is
logged and forwarded to the optional alert callback. These tests drive a fake
engine that fails on ``begin()`` so the isolation contract is proven without a
database.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from factory_agent.domain import TenantId
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.ports import UsageEvent

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


class FailingEngine:
    """AsyncEngine stand-in whose transactions always fail."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def begin(self) -> Any:
        raise self._error


def usage_event(event_id: str = "11111111-1111-4111-8111-111111111111") -> UsageEvent:
    return UsageEvent(
        event_id=event_id,
        event_type="interaction_started",
        tenant_id=TenantId("tenant-a"),
        payload={"event_type": "interaction_started"},
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_write_failure_is_logged_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    store = SqlMeteringStore(FailingEngine(RuntimeError("connection lost")))  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="factory_agent.persistence.metering"):
        await store.write_usage_events([usage_event()])  # type: ignore[arg-type]

    assert "usage.metering.write_failed" in caplog.text


@pytest.mark.asyncio
async def test_write_failure_invokes_the_alert_callback() -> None:
    alerted: list[Exception] = []
    store = SqlMeteringStore(
        FailingEngine(RuntimeError("connection lost")),  # type: ignore[arg-type]
        on_failure=alerted.append,
    )

    await store.write_usage_events([usage_event()])  # type: ignore[arg-type]

    assert len(alerted) == 1
    assert isinstance(alerted[0], RuntimeError)


@pytest.mark.asyncio
async def test_alert_callback_raising_is_swallowed() -> None:
    def broken_alert(error: Exception) -> None:
        raise ValueError("alerting failed")

    store = SqlMeteringStore(
        FailingEngine(RuntimeError("boom")),  # type: ignore[arg-type]
        on_failure=broken_alert,
    )

    # Neither the write failure nor the alerting failure is raised.
    await store.write_usage_events([usage_event()])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_empty_event_list_is_a_noop() -> None:
    store = SqlMeteringStore(FailingEngine(RuntimeError("boom")))  # type: ignore[arg-type]

    await store.write_usage_events([])  # type: ignore[arg-type]

    # No transaction was attempted, so the failing engine is never touched.
