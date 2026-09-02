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

from usage_admin.events import MesCallFact
from usage_admin.mes_categories import (
    CATEGORY_ORDERING,
    CATEGORY_OTHER,
    MesCategoryResolver,
)
from usage_admin.platform import PlatformScope, PlatformScopeError
from usage_admin.store import TenantRegistryRecord, UsageStore, hour_bucket

Granularity = Literal["hour", "day"]

#: Rollup rows are produced by factory-agent (``rollup-v2``); this
#: service only reads the pre-aggregated tables. The label below mirrors
#: ``factory_agent.application.rollup.ROLLUP_VERSION`` so report responses keep
#: identifying which aggregation version produced the numbers.
ROLLUP_VERSION = "rollup-v2"

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
    "mes_output",
    "mes_payroll",
    "mes_order",
    "mes_other",
)

#: Successful MES calls by billing category (D1/D5), computed from
#: ``mes_call_fact`` at query time; factory-agent's rollup pre-aggregates the
#: same metrics.
_MES_METRICS: tuple[str, ...] = (
    "mes_output",
    "mes_payroll",
    "mes_order",
    "mes_other",
)

_MES_STATUSES: tuple[str, ...] = ("completed", "failed")

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


@dataclass(frozen=True, slots=True)
class MesCategoriesView:
    """Successful MES calls grouped into the four billing categories (D1/D5)."""

    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    categories: dict[str, int]
    total: int
    metric_version: str
    timezone: str
    incomplete: bool


@dataclass(frozen=True, slots=True)
class MesFailuresView:
    """Failed MES calls (D7, independent endpoint) by category and error."""

    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    categories: dict[str, int]
    by_error: dict[str, int]
    total: int
    metric_version: str
    timezone: str


@dataclass(frozen=True, slots=True)
class MesOperationsView:
    """Per-operation_id call breakdown (success only, finer than categories)."""

    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    values: dict[str, float]
    truncated: bool
    metric_version: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ModelsView:
    """Per-actual-model call and token totals (cost attribution)."""

    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    values: dict[str, dict[str, int]]
    metric_version: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ErrorsView:
    """Per-error_category distribution (troubleshooting)."""

    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    values: dict[str, float]
    truncated: bool
    metric_version: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ByTenantItem:
    """One factory row of the usage detail table (F1.13)."""

    app_key: str
    tenant_name: str | None
    status: str | None
    token_total: int
    question_count: int
    mes_output: int
    mes_payroll: int
    mes_order: int
    mes_other: int
    last_usage_at: datetime | None


@dataclass(frozen=True, slots=True)
class ByTenantPage:
    tenant_ids: tuple[str, ...]
    start: datetime
    end: datetime
    items: list[ByTenantItem]
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
        category_resolver: MesCategoryResolver | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._timezone_name = timezone_name
        self._limits = limits or OpsLimits()
        self._categories = category_resolver or MesCategoryResolver(store)

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

        incomplete = _rollup_missing_buckets(rollup, start, end, "hour", now=self._clock())
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
        mes_requested = tuple(metric for metric in requested if metric in _MES_METRICS)
        if mes_requested:
            mes_buckets = await self._mes_category_buckets(tenant_ids, start, end, granularity)
            for bucket, bucket_metrics in mes_buckets.items():
                for metric in mes_requested:
                    by_bucket[bucket][metric] += bucket_metrics.get(metric, 0.0)
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

    async def mes_categories(
        self, scope: PlatformScope, start: datetime, end: datetime
    ) -> MesCategoriesView:
        """Successful MES calls by the four billing categories (D1/D5)."""
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return MesCategoriesView(
                (),
                start,
                end,
                _empty_categories(),
                0,
                self._metric_version(),
                self._timezone_name,
                False,
            )
        facts = await self._store.list_mes_call_facts(tenant_ids, start, end)
        resolved = await self._categories.categories_for(
            frozenset(fact.operation_id for fact in facts if fact.status == "completed")
        )
        counts: Counter[str] = Counter()
        for fact in facts:
            if fact.status == "completed":
                counts[resolved.get(fact.operation_id, CATEGORY_OTHER)] += 1
        categories = {category: counts.get(category, 0) for category in CATEGORY_ORDERING}
        return MesCategoriesView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            categories=categories,
            total=sum(categories.values()),
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
            incomplete=False,
        )

    async def mes_failures(
        self, scope: PlatformScope, start: datetime, end: datetime
    ) -> MesFailuresView:
        """Failed MES calls (D7, independent endpoint)."""
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return MesFailuresView(
                (),
                start,
                end,
                _empty_categories(),
                {},
                0,
                self._metric_version(),
                self._timezone_name,
            )
        facts = await self._store.list_mes_call_facts(tenant_ids, start, end)
        resolved = await self._categories.categories_for(
            frozenset(fact.operation_id for fact in facts if fact.status == "failed")
        )
        by_category: Counter[str] = Counter()
        by_error: Counter[str] = Counter()
        for fact in facts:
            if fact.status != "failed":
                continue
            by_category[resolved.get(fact.operation_id, CATEGORY_OTHER)] += 1
            if fact.error_category:
                by_error[fact.error_category] += 1
        categories = {category: by_category.get(category, 0) for category in CATEGORY_ORDERING}
        return MesFailuresView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            categories=categories,
            by_error=dict(by_error.most_common()),
            total=sum(categories.values()),
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
        )

    async def mes_operations(
        self, scope: PlatformScope, start: datetime, end: datetime
    ) -> MesOperationsView:
        """Successful calls per concrete ``operation_id`` (finer than category)."""
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return MesOperationsView(
                (), start, end, {}, False, self._metric_version(), self._timezone_name
            )
        facts = await self._store.list_mes_call_facts(tenant_ids, start, end)
        counts: Counter[str] = Counter(
            fact.operation_id for fact in facts if fact.status == "completed"
        )
        values = {
            key: float(value) for key, value in counts.most_common(self._limits.max_dimension_rows)
        }
        truncated = sum(counts.values()) > self._limits.max_dimension_rows
        return MesOperationsView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            values=values,
            truncated=truncated,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
        )

    async def models(self, scope: PlatformScope, start: datetime, end: datetime) -> ModelsView:
        """Per-actual-model call and token totals (cost attribution)."""
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return ModelsView((), start, end, {}, self._metric_version(), self._timezone_name)
        facts = await self._store.list_llm_call_facts(tenant_ids, start, end)
        totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for fact in facts:
            entry = totals[fact.actual_model]
            entry["calls"] += 1
            entry["prompt_tokens"] += fact.prompt_tokens
            entry["completion_tokens"] += fact.completion_tokens
            entry["cached_tokens"] += fact.cached_tokens
            entry["reasoning_tokens"] += fact.reasoning_tokens
        values = {model: dict(entry) for model, entry in sorted(totals.items())}
        return ModelsView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            values=values,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
        )

    async def capabilities(
        self, scope: PlatformScope, start: datetime, end: datetime
    ) -> DimensionsView:
        """Agent-capability distribution; complements (never replaces) MES categories."""
        return await self.dimensions(scope, start, end, "capability")

    async def errors(self, scope: PlatformScope, start: datetime, end: datetime) -> ErrorsView:
        """Error-category distribution across interaction and LLM facts."""
        self._check_range(start, end)
        tenant_ids = await self._tenant_ids(scope, start, end)
        if not tenant_ids:
            return ErrorsView(
                (), start, end, {}, False, self._metric_version(), self._timezone_name
            )
        counts: Counter[str] = Counter()
        interaction_facts = await self._store.list_interaction_facts(tenant_ids, start, end)
        for fact in interaction_facts:
            if fact.error_category:
                counts[fact.error_category] += 1
        llm_facts = await self._store.list_llm_call_facts(tenant_ids, start, end)
        for fact in llm_facts:
            if fact.error_category:
                counts[fact.error_category] += 1
        values = {
            key: float(value) for key, value in counts.most_common(self._limits.max_dimension_rows)
        }
        truncated = sum(counts.values()) > self._limits.max_dimension_rows
        return ErrorsView(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            values=values,
            truncated=truncated,
            metric_version=self._metric_version(),
            timezone=self._timezone_name,
        )

    async def by_tenant(
        self,
        scope: PlatformScope,
        start: datetime,
        end: datetime,
        *,
        limit: int | None,
        offset: int,
        name: str | None = None,
        app_key: str | None = None,
    ) -> ByTenantPage:
        """Per-factory usage detail with pagination (F1.13/F1.14, D8 status)."""
        self._check_range(start, end)
        if offset < 0:
            raise OpsQueryError("offset must not be negative")
        page_size = self._clamp_page(limit)
        registry = await self._store.list_all_tenant_registry()
        by_key = {record.app_key: record for record in registry}

        requested: set[str] = set()
        if app_key:
            requested.add(app_key)
        if name:
            requested.update(await self._store.search_tenant_registry_names(name))
        scoped_keys = await self._candidate_tenant_keys(scope, start, end, by_key)
        if requested:
            if not requested <= scoped_keys:
                raise PlatformScopeError("requested tenant set exceeds the platform scope")
            candidate_keys = requested & scoped_keys
        else:
            candidate_keys = scoped_keys

        if not candidate_keys:
            return ByTenantPage(
                (), start, end, [], 0, None, self._metric_version(), self._timezone_name
            )

        tenant_ids = frozenset(candidate_keys)
        rollup = await self._store.list_rollup_rows(tenant_ids, start, end, "hour")
        totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in rollup:
            totals[row.tenant_id][row.metric] += row.value
        mes_facts = await self._store.list_mes_call_facts(tenant_ids, start, end)
        mes_by_tenant = await self._successful_mes_by_tenant(mes_facts, tenant_ids)
        freshness_by_tenant = {
            tenant_id: await self._store.query_freshness(frozenset({tenant_id}), start, end)
            for tenant_id in candidate_keys
        }

        rows: list[ByTenantItem] = []
        for tenant_id in sorted(candidate_keys):
            record = by_key.get(tenant_id)
            mes = mes_by_tenant.get(tenant_id, _empty_categories())
            token_total = int(
                sum(
                    totals[tenant_id].get(metric, 0.0)
                    for metric in (
                        "prompt_tokens",
                        "completion_tokens",
                        "cached_tokens",
                        "reasoning_tokens",
                    )
                )
            )
            rows.append(
                ByTenantItem(
                    app_key=tenant_id,
                    tenant_name=record.tenant_name if record else None,
                    status=record.status if record else None,
                    token_total=token_total,
                    question_count=int(totals[tenant_id].get("questions", 0.0)),
                    mes_output=mes[CATEGORY_ORDERING[0]],
                    mes_payroll=mes[CATEGORY_ORDERING[1]],
                    mes_order=mes[CATEGORY_ORDERING[2]],
                    mes_other=mes[CATEGORY_ORDERING[3]],
                    last_usage_at=freshness_by_tenant.get(tenant_id),
                )
            )

        page = rows[offset : offset + page_size]
        next_cursor = offset + len(page) if offset + len(page) < len(rows) else None
        return ByTenantPage(
            tenant_ids=tuple(sorted(tenant_ids)),
            start=start,
            end=end,
            items=page,
            total=len(rows),
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
        return f"rollup={ROLLUP_VERSION};p={PERCENTILE_METHOD}"

    async def _candidate_tenant_keys(
        self,
        scope: PlatformScope,
        start: datetime,
        end: datetime,
        by_key: dict[str, TenantRegistryRecord],
    ) -> set[str]:
        """Registry accounts plus data-carrying tenants, all within the scope."""
        if scope.tenant_ids:
            return set(scope.tenant_ids)
        return set(by_key) | set(await self._store.list_tenants(start, end))

    async def _successful_mes_by_tenant(
        self, facts: list[MesCallFact], tenant_ids: frozenset[str]
    ) -> dict[str, dict[str, int]]:
        resolved = await self._categories.categories_for(
            frozenset(fact.operation_id for fact in facts if fact.status == "completed")
        )
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for fact in facts:
            if fact.status != "completed":
                continue
            counts[fact.tenant_id][resolved.get(fact.operation_id, CATEGORY_OTHER)] += 1
        return {
            tenant_id: {
                category: counts[tenant_id].get(category, 0) for category in CATEGORY_ORDERING
            }
            for tenant_id in tenant_ids
        }

    async def _mes_category_buckets(
        self,
        tenant_ids: frozenset[str],
        start: datetime,
        end: datetime,
        granularity: Granularity,
    ) -> dict[datetime, dict[str, float]]:
        facts = await self._store.list_mes_call_facts(tenant_ids, start, end)
        completed = [fact for fact in facts if fact.status == "completed"]
        if not completed:
            return {}
        resolved = await self._categories.categories_for(
            frozenset(fact.operation_id for fact in completed)
        )
        buckets: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for fact in completed:
            key = _bucket_start(fact.occurred_at, granularity)
            category = resolved.get(fact.operation_id, CATEGORY_OTHER)
            buckets[key][f"mes_{category}"] += 1.0
        return {bucket: dict(metrics) for bucket, metrics in buckets.items()}


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
    rows: list[Any],
    start: datetime,
    end: datetime,
    granularity: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Rollup coverage missing for any expected bucket is an explicit incomplete state."""
    if not rows:
        return True
    if granularity != "hour":
        return False
    active_now = now or datetime.now(timezone.utc)
    current_hour = hour_bucket(active_now)
    covered = {getattr(row, "bucket_start", None) for row in rows}
    bucket = hour_bucket(start)
    while bucket < hour_bucket(end) and bucket < current_hour:
        if bucket not in covered:
            return True
        bucket += timedelta(hours=1)
    return False


def _empty_categories() -> dict[str, int]:
    return {category: 0 for category in CATEGORY_ORDERING}


def _bucket_start(occurred_at: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return hour_bucket(occurred_at)
    return occurred_at.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = [
    "ByTenantItem",
    "ByTenantPage",
    "DurationStats",
    "ErrorsView",
    "MesCategoriesView",
    "MesFailuresView",
    "MesOperationsView",
    "ModelsView",
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
