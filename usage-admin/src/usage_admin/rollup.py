"""Replayable hourly/daily rollup.

The rollup engine is pure against the ``UsageStore`` protocol: it lists the
facts in a window, groups them into hour/day buckets, and upserts idempotent
rollup rows stamped with a rollup version. Raw facts remain the source of
truth, so any window can be recomputed.

The worker acquires a PostgreSQL advisory lock so multiple replicas can run the
rollup safely side by side.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from usage_admin.store import RollupRow, UsageStore, hour_bucket

_LOGGER = logging.getLogger("usage_admin.rollup")

ROLLUP_VERSION = "rollup-v1"

#: Additive metrics computed from interaction facts (per bucket).
_INTERACTION_METRICS: tuple[str, ...] = (
    "questions",
    "valid_questions",
    "e2e_duration_ms",
    "mes_duration_ms",
    "llm_duration_ms",
    "local_duration_ms",
)

#: Additive metrics computed from LLM call facts (per bucket).
_LLM_METRICS: tuple[str, ...] = (
    "llm_physical_attempts",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
)

_STATUS_LABELS: tuple[str, ...] = ("completed", "failed", "cancelled", "rejected")


def _base_bucket() -> dict[str, float]:
    metrics: dict[str, float] = {metric: 0.0 for metric in _INTERACTION_METRICS}
    metrics.update({metric: 0.0 for metric in _LLM_METRICS})
    metrics.update({f"status.{status}": 0.0 for status in _STATUS_LABELS})
    metrics.update({f"{metric}.count": 0.0 for metric in _INTERACTION_METRICS})
    metrics.update({f"{metric}.count": 0.0 for metric in _LLM_METRICS})
    metrics["users"] = 0.0
    metrics["llm_logical_calls"] = 0.0
    return metrics


def _bucket_key(occurred_at: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return hour_bucket(occurred_at)
    return _midnight(occurred_at.astimezone(timezone.utc).date())


#: Duration metric name -> fact attribute name (the e2e duration is ``duration_ms``).
_DURATION_FIELD_BY_METRIC: dict[str, str] = {
    "e2e_duration_ms": "duration_ms",
    "mes_duration_ms": "mes_duration_ms",
    "llm_duration_ms": "llm_duration_ms",
    "local_duration_ms": "local_duration_ms",
}


def compute_bucket_metrics(
    interaction_facts: list[Any],
    llm_facts: list[Any],
    granularity: str,
) -> dict[datetime, dict[str, float]]:
    """Group additive metrics into buckets keyed by their UTC bucket start."""
    buckets: dict[datetime, dict[str, float]] = defaultdict(_base_bucket)
    users: dict[datetime, set[str]] = defaultdict(set)
    calls: dict[datetime, set[str]] = defaultdict(set)
    for fact in interaction_facts:
        key = _bucket_key(fact.occurred_at, granularity)
        if getattr(fact, "event_type", "") == "interaction_started":
            buckets[key]["questions"] += 1
            if getattr(fact, "capability_id", None) is not None:
                buckets[key]["valid_questions"] += 1
        subject = getattr(fact, "user_subject_id", "")
        if subject:
            users[key].add(subject)
        status = getattr(fact, "status", None)
        if status and f"status.{status}" in buckets[key]:
            buckets[key][f"status.{status}"] += 1
        for metric, field in _DURATION_FIELD_BY_METRIC.items():
            value = getattr(fact, field, None)
            if isinstance(value, (int, float)):
                buckets[key][metric] += float(value)
                buckets[key][f"{metric}.count"] += 1
    for fact in llm_facts:
        key = _bucket_key(fact.occurred_at, granularity)
        buckets[key]["llm_physical_attempts"] += 1
        logical_call_id = getattr(fact, "logical_call_id", "")
        if logical_call_id:
            calls[key].add(logical_call_id)
        for metric in _LLM_METRICS:
            value = getattr(fact, metric, None)
            if isinstance(value, (int, float)):
                buckets[key][metric] += float(value)
                buckets[key][f"{metric}.count"] += 1
    for key, bucket in buckets.items():
        bucket["users"] = float(len(users[key]))
        bucket["llm_logical_calls"] = float(len(calls[key]))
    return dict(buckets)


@dataclass(frozen=True, slots=True)
class RollupRun:
    tenant_ids: frozenset[str]
    start: datetime
    end: datetime
    hourly_rows: int
    daily_rows: int


class RollupEngine:
    """Recomputes hourly/daily rollup rows for a tenant/window slice."""

    def __init__(
        self,
        store: UsageStore,
        *,
        clock: Callable[[], datetime],
        version: str = ROLLUP_VERSION,
    ) -> None:
        self._store = store
        self._clock = clock
        self._version = version

    async def rollup_range(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> RollupRun:
        if not tenant_ids or start >= end:
            return RollupRun(tenant_ids, start, end, 0, 0)
        interaction_facts = await self._store.list_interaction_facts(tenant_ids, start, end)
        llm_facts = await self._store.list_llm_call_facts(tenant_ids, start, end)

        now = self._clock()
        rows: list[RollupRow] = []
        hour_buckets: set[datetime] = set()
        day_buckets: set[datetime] = set()
        for tenant_id in tenant_ids:
            tenant_interactions = [
                fact for fact in interaction_facts if fact.tenant_id == tenant_id
            ]
            tenant_llm = [fact for fact in llm_facts if fact.tenant_id == tenant_id]
            hourly = compute_bucket_metrics(tenant_interactions, tenant_llm, "hour")
            daily = compute_bucket_metrics(tenant_interactions, tenant_llm, "day")
            hour_buckets.update(hourly)
            day_buckets.update(daily)
            for bucket, metrics in hourly.items():
                for metric, value in metrics.items():
                    rows.append(_row(tenant_id, bucket, metric, value, self._version, now, "hour"))
            for bucket, metrics in daily.items():
                for metric, value in metrics.items():
                    rows.append(_row(tenant_id, bucket, metric, value, self._version, now, "day"))

        await self._store.upsert_rollup_rows(rows)
        return RollupRun(tenant_ids, start, end, len(hour_buckets), len(day_buckets))


def _row(
    tenant_id: str,
    bucket: datetime,
    metric: str,
    value: float,
    version: str,
    now: datetime,
    granularity: str,
) -> RollupRow:
    return RollupRow(
        tenant_id=tenant_id,
        bucket_start=bucket,
        metric=metric,
        value=value,
        rollup_version=version,
        rolled_up_at=now,
        granularity=granularity,
    )


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


class RollupWorker:
    """Runs rollups on an interval; the caller holds the advisory lock."""

    def __init__(
        self,
        engine: RollupEngine,
        *,
        tenant_ids: frozenset[str] = frozenset(),
        poll_seconds: float = 60.0,
        window_hours: int = 24,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._engine = engine
        self._tenant_ids = tenant_ids
        self._poll_seconds = poll_seconds
        self._window_hours = window_hours
        self._sleep = sleep or asyncio.sleep

    async def run_once(self, now: datetime) -> RollupRun:
        start = now - timedelta(hours=self._window_hours)
        return await self._engine.rollup_range(self._tenant_ids, start, now)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.run_once(datetime.now(timezone.utc))
            except Exception:  # noqa: BLE001 - the worker must survive transient faults
                _LOGGER.exception("usage.rollup.cycle_failed")
            await self._sleep(self._poll_seconds)


class AdvisoryLock:
    """Holds a PostgreSQL advisory lock for the duration of a context.

    Used only by the rollup worker process; unit tests never open a real
    connection.
    """

    def __init__(self, database_url: str, *, lock_key: int = 826_271) -> None:
        self._database_url = database_url
        self._lock_key = lock_key
        self._connection: Any | None = None

    async def __aenter__(self) -> "AdvisoryLock":
        import psycopg

        self._connection = await psycopg.AsyncConnection.connect(self._database_url)
        await self._connection.execute("SELECT pg_advisory_lock(%s)", (self._lock_key,))
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._connection is not None:
            try:
                await self._connection.execute("SELECT pg_advisory_unlock(%s)", (self._lock_key,))
            finally:
                await self._connection.close()


__all__ = [
    "AdvisoryLock",
    "ROLLUP_VERSION",
    "RollupEngine",
    "RollupRun",
    "RollupWorker",
    "compute_bucket_metrics",
]
