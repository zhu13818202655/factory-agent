"""Platform operations queries and report service.

Every query is bounded by the caller's ``PlatformScope`` (tenant set) plus time
range, pagination, span, and row-count budgets. Over-limit requests fail with a
structured ``OpsQueryError`` rather than returning a partial or widened answer.
Responses carry the metric version, the tenant timezone, data freshness, and an
explicit incomplete state so consumers never mistake a bounded result for a
complete one.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from usage_admin.platform import PlatformScope
from usage_admin.rollup import ROLLUP_VERSION
from usage_admin.store import UsageStore, hour_bucket

Granularity = Literal["hour", "day"]

_DURATION_METRICS: tuple[str, ...] = (
    "e2e_duration_ms",
    "mes_duration_ms",
    "llm_duration_ms",
    "local_duration_ms",
)

#: Additive rollup metrics exposed by summary/timeseries.
_ADDITIVE_METRICS: tuple[str, ...] = (
    "users",
    "questions",
    "valid_questions",
    "status.completed",
    "status.failed",
    "status.cancelled",
    "status.rejected",
    "llm_logical_calls",
    "llm_physical_attempts",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
)

_STATUS_METRICS: tuple[str, ...] = (
    "status.completed",
    "status.failed",
    "status.cancelled",
    "status.rejected",
)

_TOKEN_METRICS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
)

#: Dimensions that can be reported over interaction facts.
_INTERACTION_DIMENSIONS: tuple[str, ...] = (
    "capability",
    "status",
    "entrypoint",
    "role_category",
    "error_category",
)

#: Dimensions that can be reported over LLM call facts.
_LLM_DIMENSIONS: tuple[str, ...] = (
    "model_alias",
    "actual_model",
    "stage",
    "fallback_reason",
    "error_category",
)

CONTRACT_VERSION = "usage-events-v1"
PERCENTILE_METHOD = "percentile-cont-v1"


class OpsQueryError(ValueError):
    """Structured rejection for an over-limit or malformed platform query."""


@dataclass(frozen=True, slots=True)
class OpsLimits:
    max_span_days: int = 366
    max_dimension_rows: int = 1000
    user_page_size: int = 50
    user_page_max: int = 200
    timeseries_max_buckets: int = 10_000


@dataclass(frozen=True, slots=True)
class DurationStats:
    count: int
    mean_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None


@dataclass(frozen=True, slots=True)
class SummaryView:
    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    users: int
    questions: int
    valid_questions: int
    status: dict[str, int]
    llm_logical_calls: int
    llm_physical_attempts: int
    tokens: dict[str, int]
    durations: dict[str, DurationStats]
    metric_version: str
    timezone: str
    freshness: datetime | None
    incomplete: bool


@dataclass(frozen=True, slots=True)
class TimeseriesPoint:
    bucket: datetime
    metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class TimeseriesView:
    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    granularity: Granularity
    points: list[TimeseriesPoint]
    metric_version: str
    timezone: str
    incomplete: bool


@dataclass(frozen=True, slots=True)
class DimensionsView:
    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    dimension: str
    values: dict[str, float]
    truncated: bool
    metric_version: str
    timezone: str


@dataclass(frozen=True, slots=True)
class UserActivity:
    user_subject_id: str
    question_count: int


@dataclass(frozen=True, slots=True)
class UsersPage:
    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    items: list[UserActivity]
    total: int
    next_cursor: int | None
    metric_version: str
    timezone: str


class OpsService:
    """Bounded platform report queries over a ``UsageStore``."""

    def __init__(
        self,
        store: UsageStore,
        *,
        clock: Callable[[], datetime],
        timezone_name: str = "Asia/Shanghai",
        limits: OpsLimits | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._timezone_name = timezone_name
        self._limits = limits or OpsLimits()

    async def list_tenants(self, scope: PlatformScope, start: datetime, end: datetime) -> list[str]:
        self._check_range(start, end)
        tenants = await self._store.list_tenants(start, end)
        return [tenant for tenant in tenants if scope.covers_tenant(tenant)]

    async def _tenant_ids(
        self, scope: PlatformScope, start: datetime, end: datetime
    ) -> frozenset[str]:
        """Resolve the effective tenant set for a platform-wide principal."""
        if scope.tenant_ids:
            return scope.tenant_ids
        return frozenset(await self._store.list_tenants(start, end))

    async def summary(self, scope: PlatformScope, start: datetime, end: datetime) -> SummaryView:
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return self._empty_summary(start, end)
        rollup = await self._store.list_rollup_rows(tenant_ids, start, end, "hour")
        totals: dict[str, float] = defaultdict(float)
        for row in rollup:
            totals[row.metric] += row.value

        distinct = await self._store.query_distinct_counts(tenant_ids, start, end)
        percentiles = await self._store.query_duration_percentiles(tenant_ids, start, end)
        freshness = await self._store.query_freshness(tenant_ids, start, end)

        durations: dict[str, DurationStats] = {}
        for metric in _DURATION_METRICS:
            count = int(totals.get(f"{metric}.count", 0.0)) if f"{metric}.count" in totals else 0
            mean = _mean_ms(totals, metric)
            p = percentiles.get(metric, {})
            durations[metric] = DurationStats(
                count=count,
                mean_ms=mean,
                p50_ms=p.get("50"),
                p95_ms=p.get("95"),
                p99_ms=p.get("99"),
            )

        incomplete = _rollup_missing_buckets(rollup, start, end, "hour")
        return SummaryView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            users=distinct.get("users", 0),
            questions=int(totals.get("questions", 0.0)),
            valid_questions=int(totals.get("valid_questions", 0.0)),
            status={label: int(totals.get(label, 0.0)) for label in _STATUS_METRICS},
            llm_logical_calls=distinct.get("llm_logical_calls", 0),
            llm_physical_attempts=int(totals.get("llm_physical_attempts", 0.0)),
            tokens={label: int(totals.get(label, 0.0)) for label in _TOKEN_METRICS},
            durations=durations,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
            freshness=freshness,
            incomplete=incomplete,
        )

    async def timeseries(
        self,
        scope: PlatformScope,
        start: datetime,
        end: datetime,
        granularity: Granularity,
        metrics: tuple[str, ...],
    ) -> TimeseriesView:
        self._check_range(start, end)
        requested = tuple(metric for metric in metrics if metric in _ADDITIVE_METRICS)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return TimeseriesView(
                (), start, end, granularity, [], self._metric_version(), self._timezone_name, False
            )
        rows = await self._store.list_rollup_rows(tenant_ids, start, end, granularity)
        by_bucket: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in rows:
            by_bucket[row.bucket_start][row.metric] += row.value
        points = [
            TimeseriesPoint(
                bucket=bucket,
                metrics={metric: by_bucket[bucket].get(metric, 0.0) for metric in requested},
            )
            for bucket in sorted(by_bucket)
        ][: self._limits.timeseries_max_buckets]
        truncated = len(points) >= self._limits.timeseries_max_buckets
        return TimeseriesView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            granularity=granularity,
            points=points,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
            incomplete=truncated,
        )

    async def dimensions(
        self,
        scope: PlatformScope,
        start: datetime,
        end: datetime,
        dimension: str,
    ) -> DimensionsView:
        self._check_range(start, end)
        if dimension not in _INTERACTION_DIMENSIONS and dimension not in _LLM_DIMENSIONS:
            raise OpsQueryError(f"unsupported dimension {dimension!r}")
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return DimensionsView(
                (), start, end, dimension, {}, False, self._metric_version(), self._timezone_name
            )
        counts: Counter[str] = Counter()
        if dimension in _INTERACTION_DIMENSIONS:
            facts = await self._store.list_interaction_facts(tenant_ids, start, end)
            for fact in facts:
                value = getattr(fact, _fact_attr(dimension), None)
                if value:
                    counts[str(value)] += 1
        else:
            facts = await self._store.list_llm_call_facts(tenant_ids, start, end)
            for fact in facts:
                value = getattr(fact, _fact_attr(dimension), None)
                if value:
                    counts[str(value)] += 1
        values = {
            key: float(value) for key, value in counts.most_common(self._limits.max_dimension_rows)
        }
        truncated = sum(counts.values()) > self._limits.max_dimension_rows
        return DimensionsView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            dimension=dimension,
            values=values,
            truncated=truncated,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
        )

    async def users(
        self,
        scope: PlatformScope,
        start: datetime,
        end: datetime,
        limit: int | None,
        offset: int = 0,
    ) -> UsersPage:
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        page_size = self._clamp_page(limit)
        if offset < 0:
            raise OpsQueryError("offset must not be negative")
        if not tenant_ids:
            return UsersPage(
                (), start, end, [], 0, None, self._metric_version(), self._timezone_name
            )
        pairs, total = await self._store.query_user_activity(
            tenant_ids, start, end, page_size, offset
        )
        items = [UserActivity(user_subject_id=pair[0], question_count=pair[1]) for pair in pairs]
        next_cursor = offset + len(items) if offset + len(items) < total else None
        return UsersPage(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            items=items,
            total=total,
            next_cursor=next_cursor,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
        )

    def metric_version(self) -> str:
        return self._metric_version()

    def _check_range(self, start: datetime, end: datetime) -> None:
        if start >= end:
            raise OpsQueryError("time range must have start before end")
        span = end - start
        if span > timedelta(days=self._limits.max_span_days):
            raise OpsQueryError(
                f"time range exceeds the {self._limits.max_span_days}-day span budget"
            )

    def _clamp_page(self, limit: int | None) -> int:
        if limit is None:
            return self._limits.user_page_size
        if limit < 1:
            raise OpsQueryError("page size must be at least 1")
        return min(limit, self._limits.user_page_max)

    def _empty_summary(self, start: datetime, end: datetime) -> SummaryView:
        return SummaryView(
            tenant_ids=(),
            start=start,
            end=end,
            users=0,
            questions=0,
            valid_questions=0,
            status={label: 0 for label in _STATUS_METRICS},
            llm_logical_calls=0,
            llm_physical_attempts=0,
            tokens={label: 0 for label in _TOKEN_METRICS},
            durations={metric: DurationStats(0) for metric in _DURATION_METRICS},
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
            freshness=None,
            incomplete=False,
        )

    def _metric_version(self) -> str:
        return f"rollup={ROLLUP_VERSION};contract={CONTRACT_VERSION};p={PERCENTILE_METHOD}"


def _mean_ms(totals: dict[str, float], metric: str) -> float | None:
    count = totals.get(f"{metric}.count", 0.0)
    total = totals.get(metric, 0.0)
    if count <= 0:
        return None
    return total / count


def _fact_attr(dimension: str) -> str:
    return {
        "capability": "capability_id",
        "status": "status",
        "entrypoint": "entrypoint",
        "role_category": "role_category",
        "error_category": "error_category",
        "model_alias": "model_alias",
        "actual_model": "actual_model",
        "stage": "stage",
        "fallback_reason": "fallback_reason",
    }.get(dimension, dimension)


def _rollup_missing_buckets(
    rows: list[Any], start: datetime, end: datetime, granularity: str
) -> bool:
    """Rollup coverage missing for any expected bucket is an explicit incomplete state."""
    if not rows:
        return True
    if granularity != "hour":
        return False
    now = datetime.now(timezone.utc)
    current_hour = hour_bucket(now)
    covered = {getattr(row, "bucket_start", None) for row in rows}
    bucket = hour_bucket(start)
    while bucket < hour_bucket(end) and bucket < current_hour:
        if bucket not in covered:
            return True
        bucket += timedelta(hours=1)
    return False


__all__ = [
    "CONTRACT_VERSION",
    "DurationStats",
    "OpsLimits",
    "OpsQueryError",
    "OpsService",
    "PERCENTILE_METHOD",
    "SummaryView",
    "TimeseriesView",
    "DimensionsView",
    "UsersPage",
    "UserActivity",
]
