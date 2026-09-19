"""Replayable hourly/daily rollup owned by this service.

Raw facts remain the source of truth; the rollup engine groups them into hour
and day buckets and upserts idempotent rows stamped with a rollup version. The
engine is pure against a ``RollupStore`` protocol so unit tests run offline.

MES category metrics: ``mes_call_fact`` rows are classified by
joining ``mes_operation_category`` (reviewed from ``apis.yaml``), and counts
are aggregated per category and per status — success and failure are kept
separate. Call counts come from fact row counts; ``page_count`` is never summed
(D6).
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.rollup import (
    InteractionFactRow,
    LlmCallFactRow,
    MesCallFactRow,
    RollupRow,
)

_LOGGER = get_logger("factory_agent.application.rollup")

#: Bumped whenever the metric set changes so old windows are recomputed under a
#: new version instead of being silently overwritten.
#: v3: ``valid_questions`` moved from the start event to the routed event (the
#: capability is only known after routing), so v2 windows must be recomputed.
ROLLUP_VERSION = "rollup-v3"

#: Billing categories from ``mes_operation_category`` / ``apis.yaml`` (D5).
MES_CATEGORIES: tuple[str, ...] = ("output", "payroll", "order", "other")

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

_INTERACTION_STATUS_LABELS: tuple[str, ...] = ("completed", "failed", "cancelled", "rejected")

#: Duration metric name -> fact attribute name.
_DURATION_FIELD_BY_METRIC: dict[str, str] = {
    "e2e_duration_ms": "duration_ms",
    "mes_duration_ms": "mes_duration_ms",
    "llm_duration_ms": "llm_duration_ms",
    "local_duration_ms": "local_duration_ms",
}


def _base_bucket() -> dict[str, float]:
    metrics: dict[str, float] = {metric: 0.0 for metric in _INTERACTION_METRICS}
    metrics.update({metric: 0.0 for metric in _LLM_METRICS})
    metrics.update({f"status.{status}": 0.0 for status in _INTERACTION_STATUS_LABELS})
    metrics.update({f"{metric}.count": 0.0 for metric in _INTERACTION_METRICS})
    metrics.update({f"{metric}.count": 0.0 for metric in _LLM_METRICS})
    metrics["users"] = 0.0
    metrics["llm_logical_calls"] = 0.0
    # MES call metrics: total, per status, and per category (D5/D6).
    metrics["mes_calls"] = 0.0
    metrics["mes_calls.completed"] = 0.0
    metrics["mes_calls.failed"] = 0.0
    for category in MES_CATEGORIES:
        metrics[f"mes_calls.{category}"] = 0.0
    return metrics


def hour_bucket(occurred_at: datetime) -> datetime:
    return occurred_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _bucket_key(occurred_at: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return hour_bucket(occurred_at)
    return _midnight(occurred_at.astimezone(timezone.utc).date())


def compute_bucket_metrics(
    interaction_facts: list[InteractionFactRow],
    llm_facts: list[LlmCallFactRow],
    mes_facts: list[MesCallFactRow],
    categories: dict[str, str],
    granularity: str,
) -> dict[datetime, dict[str, float]]:
    """Group additive metrics into buckets keyed by their UTC bucket start."""
    buckets: dict[datetime, dict[str, float]] = defaultdict(_base_bucket)
    users: dict[datetime, set[str]] = defaultdict(set)
    calls: dict[datetime, set[str]] = defaultdict(set)
    for fact in interaction_facts:
        key = _bucket_key(fact.occurred_at, granularity)
        if fact.event_type == "interaction_started":
            buckets[key]["questions"] += 1
        elif fact.event_type == "interaction_routed" and fact.capability_id is not None:
            # 有效提问 = 路由阶段真的解析出了能力。未识别的提问记进 questions 但
            # 不进 valid_questions，于是两者的差就是「没被理解的提问」。
            buckets[key]["valid_questions"] += 1
        if fact.user_subject_id:
            users[key].add(fact.user_subject_id)
        status = fact.status
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
        if fact.logical_call_id:
            calls[key].add(fact.logical_call_id)
        for metric in _LLM_METRICS:
            value = getattr(fact, metric, None)
            if isinstance(value, (int, float)):
                buckets[key][metric] += float(value)
                buckets[key][f"{metric}.count"] += 1
    for fact in mes_facts:
        key = _bucket_key(fact.occurred_at, granularity)
        buckets[key]["mes_calls"] += 1
        status = "completed" if fact.status == "completed" else "failed"
        buckets[key][f"mes_calls.{status}"] += 1
        category = categories.get(fact.operation_id, "other")
        buckets[key][f"mes_calls.{category}"] += 1
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


class RollupAlertSink(Protocol):
    """Structural twin of ``statistics.alerts.AlertSink``.

    ``application`` may not import ``statistics`` (package boundaries), so the
    worker declares the only shape it uses and the composition root passes a
    real sink. Alerts carry metadata only — never fact payloads.
    """

    async def alert(self, kind: str, detail: dict[str, object]) -> None: ...


class RollupStore(Protocol):
    """Durable side of the rollup engine; implemented by ``SqlRollupStore``."""

    async def list_tenant_ids(self, start: datetime, end: datetime) -> frozenset[str]: ...

    async def list_facts(
        self,
        tenant_ids: frozenset[str],
        start: datetime,
        end: datetime,
    ) -> tuple[list[InteractionFactRow], list[LlmCallFactRow], list[MesCallFactRow]]: ...

    async def list_mes_categories(self) -> dict[str, str]: ...

    async def upsert_rollup_rows(self, rows: list[RollupRow]) -> None: ...


class RollupEngine:
    """Recomputes hourly/daily rollup rows for a tenant/window slice."""

    def __init__(
        self,
        store: RollupStore,
        *,
        clock: Callable[[], datetime],
        version: str = ROLLUP_VERSION,
    ) -> None:
        self._store = store
        self._clock = clock
        self._version = version

    async def discover_tenants(self, start: datetime, end: datetime) -> frozenset[str]:
        """Tenants that produced facts in the window.

        A worker with no configured tenant list asks the store instead of being
        handed one: a fixed list silently stops covering a tenant that starts
        sending traffic later, which is every factory that is onboarded.
        """
        return await self._store.list_tenant_ids(start, end)

    async def rollup_range(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> RollupRun:
        if not tenant_ids or start >= end:
            return RollupRun(tenant_ids, start, end, 0, 0)
        interaction_facts, llm_facts, mes_facts = await self._store.list_facts(
            tenant_ids, start, end
        )
        categories = await self._store.list_mes_categories()

        now = self._clock()
        rows: list[RollupRow] = []
        hour_buckets: set[datetime] = set()
        day_buckets: set[datetime] = set()
        for tenant_id in tenant_ids:
            tenant_interactions = [f for f in interaction_facts if f.tenant_id == tenant_id]
            tenant_llm = [f for f in llm_facts if f.tenant_id == tenant_id]
            tenant_mes = [f for f in mes_facts if f.tenant_id == tenant_id]
            hourly = compute_bucket_metrics(
                tenant_interactions, tenant_llm, tenant_mes, categories, "hour"
            )
            daily = compute_bucket_metrics(
                tenant_interactions, tenant_llm, tenant_mes, categories, "day"
            )
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


class RollupWorker:
    """Runs rollups on an interval; the caller holds the advisory lock."""

    def __init__(
        self,
        engine: RollupEngine,
        *,
        tenant_ids: frozenset[str] = frozenset(),
        poll_seconds: float = 60.0,
        window_hours: int = 24,
        sleep: Callable[[float], Any] | None = None,
        alerts: RollupAlertSink | None = None,
    ) -> None:
        self._engine = engine
        self._tenant_ids = tenant_ids
        self._poll_seconds = poll_seconds
        self._window_hours = window_hours
        self._sleep = sleep or _default_sleep
        self._alerts = alerts

    async def run_once(self, now: datetime) -> RollupRun:
        start = now - timedelta(hours=self._window_hours)
        tenant_ids = self._tenant_ids or await self._engine.discover_tenants(start, now)
        return await self._engine.rollup_range(tenant_ids, start, now)

    async def run_forever(self, stop: Any | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.run_once(datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must survive transient faults
                _LOGGER.exception("usage.rollup.cycle_failed")
                await self._alert_failure()
            await self._sleep(self._poll_seconds)

    async def _alert_failure(self) -> None:
        """A rollup that keeps failing is invisible in the product: it reads as
        zero traffic. Alert so a stuck job is noticed without an operator
        squinting at a dashboard.
        """
        if self._alerts is None:
            return
        try:
            await self._alerts.alert(
                "usage.rollup.cycle_failed",
                {"window_hours": self._window_hours, "reason": "cycle_failed"},
            )
        except Exception:  # noqa: BLE001 - alerting is best effort
            _LOGGER.exception("usage.rollup.alert_failed")


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


__all__ = [
    "MES_CATEGORIES",
    "ROLLUP_VERSION",
    "RollupAlertSink",
    "RollupEngine",
    "RollupRun",
    "RollupStore",
    "RollupWorker",
    "compute_bucket_metrics",
    "hour_bucket",
]
