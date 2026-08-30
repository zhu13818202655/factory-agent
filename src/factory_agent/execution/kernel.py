"""Production capability runner: the Story 6 kernel that closes the slice.

Maps a ``CapabilityRunRequest`` (capability_id + narrowed filters + time range)
onto a reviewed recipe: it executes every API step through the scoped executor,
proves pagination completeness, registers validated rows into the per-interaction
read-only DuckDB sandbox, runs the reviewed local compute SQL, reconciles the
local aggregate against the MES ``footer``, and returns a typed
``CapabilityRunResult`` plus the renderable ``RenderTable``.

The kernel is the single path the remaining L1 capabilities reuse. It never
constructs customer URLs, auth headers, or unbounded calls; scope identifiers
reach the executor only through ``NarrowedFilters`` and reviewed recipe params.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, cast

from factory_agent.domain import CapabilityId, TimeRange
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.recipes import (
    BUSINESS_FILTER_KEYS,
    CapabilityRecipe,
    ParamBinding,
    RecipeRegistry,
)
from factory_agent.execution.result_table import MetricRegistry, ResultColumnMeta, ResultTable
from factory_agent.execution.sandbox_runtime import InteractionSandbox, SandboxTable
from factory_agent.ports.contracts import (
    UNAVAILABLE_VALUE,
    RenderColumn,
    RenderTable,
    ResourceFetchResult,
)
from factory_agent.ports.session import CapabilityRunRequest, CapabilityRunResult

_METADATA_TABLE = "_meta"

#: Reviewed filter params a recipe step may declare; they are never scope or
#: credential identifiers.
_FILTER_PARAM_KEYS = frozenset(
    {"scheme", "Type", "Flag", "queryFooter", "userid", "huohao", "dh", "detailId"}
)


class StepExecutor(Protocol):
    """Port over the scoped executor the kernel drives.

    ``execute_full_step`` must complete authorization before the business-data
    call and return a ``ResourceFetchResult`` with completeness proof.
    """

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> ResourceFetchResult: ...


@dataclass(frozen=True, slots=True)
class KernelSettings:
    """Conservative first-release bounds for the wage vertical slice."""

    page_size: int = 200
    #: Call budget for fan-out API steps (FR-009 batch progress). A fan-out
    #: sends one request per distinct bound value (e.g. one worktype-progress
    #: call per production order); when the data window grows to factory scale
    #: (~120 orders in a two-month window, Story 10) the budget must cover it,
    #: otherwise uncovered orders are surfaced as ``call_budget_exhausted`` and
    #: their progress ratio becomes the structured ``unavailable`` state — a
    #: number is never fabricated for the uncovered remainder.
    max_api_calls: int = 500


class KernelCapabilityRunner:
    """CapabilityRunner implementation backed by recipes, executor, and sandbox."""

    def __init__(
        self,
        executor: StepExecutor,
        recipes: RecipeRegistry,
        metrics: MetricRegistry,
        *,
        settings: KernelSettings | None = None,
        clock: Any | None = None,
        resource_columns: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._executor = executor
        self._recipes = recipes
        self._metrics = metrics
        self._settings = settings or KernelSettings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        #: operation_id -> validated row column names, so an empty fetch (e.g.
        #: a fan-out that exhausted its call budget) still registers a typed
        #: sandbox table for downstream local compute.
        self._resource_columns = resource_columns or {}

    @property
    def recipes(self) -> RecipeRegistry:
        return self._recipes

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult:
        started = time.monotonic()
        recipe = self._recipes.get(str(request.capability_id))
        filters = request.filters
        time_range = request.time_range

        with InteractionSandbox(allowed_tables=tuple(self._allowed_tables(recipe))) as sandbox:
            self._register_meta(sandbox, time_range)
            fetches, call_count = await self._fetch_api_steps(sandbox, recipe, filters, time_range)
            render_table = self._build_table(sandbox, recipe, fetches, filters, time_range)
        result = self._to_run_result(render_table, time_range, fetches, call_count, started)
        return result

    def to_render_table(self, result: CapabilityRunResult) -> RenderTable:
        """Reconstruct a renderable table from a run result (for exporters)."""
        return render_table_from_run_result(result)

    # ------------------------------------------------------------------
    # Recipe + sandbox helpers.
    # ------------------------------------------------------------------

    def _allowed_tables(self, recipe: CapabilityRecipe) -> frozenset[str]:
        tables = {step.step_id for step in recipe.steps if step.kind == "api"}
        tables.add(_METADATA_TABLE)
        return frozenset(tables)

    def _register_meta(self, sandbox: InteractionSandbox, time_range: TimeRange) -> None:
        days = _natural_days(time_range.start, time_range.end)
        today = self._clock().date().isoformat()
        sandbox.register_table(
            SandboxTable(
                name=_METADATA_TABLE,
                rows=({"days": days, "today": today},),
                columns=(("days", "INTEGER"), ("today", "VARCHAR")),
            )
        )

    async def _fetch_api_steps(
        self,
        sandbox: InteractionSandbox,
        recipe: CapabilityRecipe,
        filters: Any,
        time_range: TimeRange,
    ) -> tuple[dict[str, ResourceFetchResult], int]:
        """Execute every API step in recipe order and return fetches + call count.

        A step with ``param_bindings`` fans out one call per distinct value of
        the bound column in its dependency step's sandbox table (e.g. one
        ``WorktypeProgressQuery`` per material id), bounded by
        ``KernelSettings.max_api_calls``. Exceeding the budget skips the
        remainder and marks the fetch incomplete with
        ``call_budget_exhausted``; covered rows are never merged with invented
        numbers for the skipped remainder.
        """
        fetches: dict[str, ResourceFetchResult] = {}
        call_count = 0
        for step in recipe.steps:
            if step.kind != "api":
                continue
            if step.operation_id is None:
                raise InvalidRequestError(f"api step {step.step_id} has no operation")
            static_params = self._reviewed_params(step.params)
            if step.param_bindings:
                fetched = await self._fetch_fanned(
                    sandbox, step, static_params, filters, time_range, call_count
                )
                call_count += fetched.pages_fetched
            else:
                fetched = await self._fetch_one(
                    step.operation_id, filters, time_range, static_params
                )
                call_count += 1
            columns = self._table_columns_for(step, fetched.rows, recipe)
            sandbox.register_table(
                SandboxTable(
                    name=step.step_id,
                    rows=tuple(cast(dict[str, Any], row) for row in fetched.rows),
                    columns=columns,
                )
            )
            fetches[step.step_id] = fetched
        return fetches, call_count

    def _table_columns_for(
        self,
        step: Any,
        rows: tuple[dict[str, object], ...],
        recipe: CapabilityRecipe,
    ) -> tuple[tuple[str, str], ...]:
        """Typed sandbox columns; an empty fetch falls back to the operation
        schema so downstream compute never binds to a missing table."""
        if rows:
            return _table_columns(rows, recipe, step.step_id)
        known = self._resource_columns.get(step.operation_id or "", ())
        if known:
            return tuple((name, "VARCHAR") for name in known)
        return (("__empty__", "VARCHAR"),)

    async def _fetch_fanned(
        self,
        sandbox: InteractionSandbox,
        step: Any,
        static_params: dict[str, str],
        filters: Any,
        time_range: TimeRange,
        call_count: int,
    ) -> ResourceFetchResult:
        """Fan out over distinct bound-column values with the call budget."""
        bindings = cast("dict[str, ParamBinding]", step.param_bindings)
        value_sets: dict[str, tuple[str, ...]] = {}
        for param, binding in bindings.items():
            from_step, column = _binding_identifiers(step.step_id, param, binding)
            sql = _distinct_values_sql(from_step, column)
            try:
                bound_rows = sandbox.execute(sql)
            except InvalidRequestError as error:
                raise InvalidRequestError(
                    f"sandbox rejected param binding for {step.step_id}: {error}"
                ) from error
            value_sets[param] = tuple(str(row[0]) for row in bound_rows)

        param_names = tuple(value_sets)
        combos: list[dict[str, str]] = [
            dict(zip(param_names, values))
            for values in itertools.product(*(value_sets[name] for name in param_names))
        ]

        budget = max(self._settings.max_api_calls - call_count, 0)
        all_rows: list[dict[str, object]] = []
        pages_fetched = 0
        footer: dict[str, str] | None = None
        complete = True
        reason: str | None = None
        covered = 0
        for combo in combos:
            if covered >= budget:
                complete = False
                reason = "call_budget_exhausted"
                break
            params = {**static_params, **combo}
            fetch = await self._fetch_one(step.operation_id, filters, time_range, params)
            pages_fetched += 1
            if fetch.footer is not None:
                footer = fetch.footer
            all_rows.extend(cast("list[dict[str, object]]", fetch.rows))
            if not fetch.complete:
                complete = False
                reason = fetch.reason or reason
            covered += 1
        return ResourceFetchResult(
            rows=tuple(all_rows),
            total=len(all_rows),
            pages_fetched=pages_fetched,
            complete=complete,
            reason=reason,
            footer=footer if len(combos) <= 1 else None,
        )

    async def _fetch_one(
        self,
        operation_id: str,
        filters: Any,
        time_range: TimeRange,
        extra_params: Mapping[str, str] | None = None,
    ) -> ResourceFetchResult:
        return await self._executor.execute_full_step(
            filters,
            ExecutionRequest(
                operation_id=operation_id,
                time_range=(time_range.start, time_range.end),
                pagination_size=self._settings.page_size,
            ),
            extra_params=dict(extra_params) if extra_params else None,
        )

    @staticmethod
    def _reviewed_params(params: dict[str, str] | None) -> dict[str, str]:
        """Only reviewed filter keys may pass; scope/credential keys are refused."""
        if not params:
            return {}
        unknown = set(params) - _FILTER_PARAM_KEYS
        if unknown:
            raise InvalidRequestError(f"recipe params contain non-filter keys: {sorted(unknown)}")
        return dict(params)

    # ------------------------------------------------------------------
    # Table construction.
    # ------------------------------------------------------------------

    def _build_table(
        self,
        sandbox: InteractionSandbox,
        recipe: CapabilityRecipe,
        fetches: dict[str, ResourceFetchResult],
        filters: Any,
        time_range: TimeRange,
    ) -> ResultTable:
        warnings: list[str] = []
        incomplete = False
        incomplete_reason: str | None = None

        for fetch in fetches.values():
            if not fetch.complete:
                incomplete = True
                incomplete_reason = f"pagination_{fetch.reason or 'incomplete'}"
                warnings.append(f"分页拉取未完整：{fetch.reason}")

        compute_outputs = self._run_compute_steps(sandbox, recipe, fetches, filters)
        source_ops_by_step = _source_operations(recipe)
        warnings_by_column: dict[str, str] = {}

        column_metas: list[ResultColumnMeta] = []
        unavailable_columns: set[str] = set()

        for column in recipe.result_columns:
            source_step = column.source_step
            metric = self._resolve_metric(recipe, column)
            source_ops = source_ops_by_step.get(source_step, ())
            column_metas.append(
                ResultColumnMeta(
                    name=column.name,
                    metric_name=metric.name if metric else None,
                    metric_version=metric.version if metric else None,
                    source_operations=source_ops,
                    column_type=column.column_type,
                    unit=column.unit,
                )
            )
            if metric is not None and not metric.allows_numeric_rendering():
                incomplete = True
                incomplete_reason = f"metric_unavailable:{metric.name}"
                warnings.append(f"口径未确认：{metric.name}（{metric.assumption_status}）")
                warnings_by_column[column.name] = "unavailable"
                unavailable_columns.add(column.name)

        if self._is_detail(recipe):
            rendered_rows = self._render_detail_rows(recipe, fetches)
        else:
            rendered_rows = self._render_compute_rows(recipe, compute_outputs)

        for row in rendered_rows:
            for name in unavailable_columns:
                row[name] = UNAVAILABLE_VALUE

        table_rows = tuple(
            {column.name: row.get(column.name) for column in recipe.result_columns}
            for row in rendered_rows
        )

        reconciliation: dict[str, str] | None = self._reconcile(recipe, fetches)
        if reconciliation is not None:
            incomplete = True
            incomplete_reason = "reconciliation_failed"
            warnings.append("明细合计与 footer.je_total 不一致，结果已标记为对账失败")

        totals = _build_totals(recipe, table_rows, fetches)
        return ResultTable(
            capability_id=recipe.capability_id,
            columns=tuple(column_metas),
            rows=table_rows,
            totals=totals,
            source_operations=_all_source_operations(recipe, fetches),
            warnings=tuple(warnings),
            incomplete=incomplete,
            incomplete_reason=incomplete_reason,
        )

    def _run_compute_steps(
        self,
        sandbox: InteractionSandbox,
        recipe: CapabilityRecipe,
        fetches: dict[str, ResourceFetchResult],
        filters: Any,
    ) -> dict[str, list[dict[str, object]]]:
        """Run every local step and keep ALL output rows (multi-row tables).

        ``filter_bindings`` maps a named SQL parameter to a narrowed business
        filter (``order_codes`` / ``style_codes`` / ``plan_codes``); the values
        are bound as named DuckDB parameters, never interpolated into SQL.
        """
        outputs: dict[str, list[dict[str, object]]] = {}
        for step in recipe.steps:
            if step.kind != "local":
                continue
            if step.compute is None:
                raise InvalidRequestError(f"local step {step.step_id} has no compute")
            dependency_empty = _dependency_empty(step, fetches)
            if dependency_empty:
                outputs[step.step_id] = [self._zero_aggregate(recipe, step.step_id)]
                continue
            bind_params = self._business_filter_params(step, filters)
            try:
                rows = sandbox.execute(step.compute, bind_params)
            except InvalidRequestError as error:
                raise InvalidRequestError(
                    f"sandbox rejected local compute for {step.step_id}: {error}"
                ) from error
            columns = _compute_columns(recipe, step.step_id)
            outputs[step.step_id] = [
                dict(zip(columns, row)) for row in rows if len(row) == len(columns)
            ]
        return outputs

    @staticmethod
    def _business_filter_params(step: Any, filters: Any) -> dict[str, object] | None:
        if not step.filter_bindings:
            return None
        params: dict[str, object] = {}
        for sql_param, filter_key in step.filter_bindings.items():
            if filter_key not in BUSINESS_FILTER_KEYS:
                raise InvalidRequestError(f"unknown business filter binding: {filter_key}")
            values = getattr(filters, filter_key, None)
            params[sql_param] = list(values) if values else []
        return params

    def _zero_aggregate(self, recipe: CapabilityRecipe, step_id: str) -> dict[str, object]:
        columns = _compute_columns(recipe, step_id)
        return {column: _zero_for(column, recipe) for column in columns}

    def _render_compute_rows(
        self,
        recipe: CapabilityRecipe,
        compute_outputs: dict[str, list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        """Render every row of the first local step referenced by result columns."""
        for step in recipe.steps:
            if step.kind == "local" and step.step_id in compute_outputs:
                return compute_outputs[step.step_id]
        return []

    def _render_detail_rows(
        self,
        recipe: CapabilityRecipe,
        fetches: dict[str, ResourceFetchResult],
    ) -> list[dict[str, object]]:
        api_step = next((step for step in recipe.steps if step.kind == "api"), None)
        if api_step is None or api_step.step_id not in fetches:
            return []
        rows = fetches[api_step.step_id].rows
        results: list[dict[str, object]] = []
        for row in rows:
            entry: dict[str, object] = {}
            for column in recipe.result_columns:
                raw = row.get(column.name)
                if column.column_type in ("money", "quantity") and isinstance(raw, str):
                    entry[column.name] = _decimal_or_zero(raw)
                else:
                    entry[column.name] = raw
            results.append(entry)
        return results

    def _reconcile(
        self,
        recipe: CapabilityRecipe,
        fetches: dict[str, ResourceFetchResult],
    ) -> dict[str, str] | None:
        """Compare the locally summed wage detail against the MES footer.

        The wage footer ``je_total`` is the sum of ``je`` over every detail row,
        so the local counter is the same sum over the validated rows. A mismatch
        is a structured ``reconciliation_failed`` — we never pick one side.
        """
        reconciliation = recipe.footer_reconciliation
        if not reconciliation:
            return None
        footer = _first_footer(fetches)
        if footer is None:
            return None
        api_rows = _first_api_rows(fetches)
        local_total = _sum_field(api_rows, "je")
        mismatches: dict[str, str] = {}
        for column_name, footer_field in reconciliation.items():
            remote_raw = footer.get(footer_field)
            if remote_raw is None:
                continue
            if local_total != _decimal_or_zero(remote_raw):
                mismatches[column_name] = footer_field
        return mismatches or None

    # ------------------------------------------------------------------
    # Value conversion.
    # ------------------------------------------------------------------

    def _resolve_metric(self, recipe: CapabilityRecipe, column: Any) -> Any:
        if column.metric is None:
            return None
        version = recipe.metric_versions.get(column.metric)
        if version is None:
            raise InvalidRequestError(f"result column {column.name} metric has no version")
        return self._metrics.resolve(column.metric, version)

    def _to_run_result(
        self,
        table: ResultTable,
        time_range: TimeRange,
        fetches: dict[str, ResourceFetchResult],
        call_count: int,
        started: float,
    ) -> CapabilityRunResult:
        column_names = tuple(column.name for column in table.columns)
        rows = tuple(
            tuple(_to_value(row.get(column.name), column) for column in table.columns)
            for row in table.rows
        )
        return CapabilityRunResult(
            capability_id=CapabilityId(table.capability_id),
            column_names=column_names,
            rows=rows,
            totals=table.totals,
            source_operations=table.source_operations,
            incomplete=table.incomplete,
            incomplete_reason=table.incomplete_reason,
            api_call_count=call_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            column_types={
                column.name: column.column_type
                for column in table.columns
                if column.column_type is not None
            },
            column_units={
                column.name: column.unit for column in table.columns if column.unit is not None
            },
            warnings=table.warnings,
        )

    @staticmethod
    def _is_detail(recipe: CapabilityRecipe) -> bool:
        return any(
            column.source_step == step.step_id
            for step in recipe.steps
            if step.kind == "api"
            for column in recipe.result_columns
        )


# ----------------------------------------------------------------------
# Pure helpers.
# ----------------------------------------------------------------------


def render_table_from_run_result(result: CapabilityRunResult) -> RenderTable:
    """Reconstruct a renderable table from a run result (for exporters)."""
    columns = tuple(
        RenderColumn(
            name=name,
            metric_name=None,
            metric_version=None,
            source_operations=result.source_operations,
            column_type=(result.column_types or {}).get(name),
            unit=(result.column_units or {}).get(name),
        )
        for name in result.column_names
    )
    return RenderTable(
        capability_id=str(result.capability_id),
        columns=columns,
        rows=tuple(
            {name: value for name, value in zip(result.column_names, row, strict=False)}
            for row in result.rows
        ),
        totals=result.totals,
        source_operations=result.source_operations,
        warnings=result.warnings,
        incomplete=result.incomplete,
        incomplete_reason=result.incomplete_reason,
    )


def _binding_identifiers(step_id: str, param: str, binding: ParamBinding) -> tuple[str, str]:
    """Defensive identifier whitelist before any sandbox SQL construction."""
    for value in (binding.from_step, binding.column):
        if (
            not value
            or not (value[0].isalpha() or value[0] == "_")
            or not all(char.isalnum() or char == "_" for char in value[1:])
        ):
            raise InvalidRequestError(f"step {step_id} param {param} binds an unsafe identifier")
    return binding.from_step, binding.column


def _distinct_values_sql(from_step: str, column: str) -> str:
    """Build the read-only distinct-value query for one param binding.

    Both identifiers are whitelisted to plain ``[A-Za-z_][A-Za-z0-9_]*`` names
    by ``_binding_identifiers`` before this is called, so the interpolation can
    never carry an attacker-controlled fragment.
    """
    return f'SELECT DISTINCT "{column}" FROM "{from_step}" WHERE "{column}" IS NOT NULL'  # nosec B608 - identifiers whitelisted; read-only DuckDB


def _natural_days(start: Any, end: Any) -> int:
    days = (end.date() - start.date()).days
    return max(days, 1)


def _table_columns(
    rows: tuple[dict[str, object], ...],
    recipe: CapabilityRecipe,
    step_id: str,
) -> tuple[tuple[str, str], ...]:
    names: list[str] = []
    for row in rows:
        for key in row:
            if key not in names:
                names.append(key)
    for column in recipe.result_columns:
        if column.source_step == step_id and column.name not in names:
            names.append(column.name)
    return tuple((name, "VARCHAR") for name in names)


def _compute_columns(recipe: CapabilityRecipe, step_id: str) -> list[str]:
    columns = [column.name for column in recipe.result_columns if column.source_step == step_id]
    return columns or ["value"]


def _zero_for(column: str, recipe: CapabilityRecipe) -> object:
    return Decimal("0")


def _dependency_empty(step: Any, fetches: dict[str, ResourceFetchResult]) -> bool:
    """Every api dependency fetched zero rows → the local step outputs zeros.

    Multi-step recipes (Story 7) keep computing when only one optional source
    (e.g. WskQuery) is empty; a fully void result still degrades to a zero
    aggregate instead of a fabricated number.
    """
    if not step.depends_on:
        return False
    return all(
        fetches.get(dependency) is None or not fetches[dependency].rows
        for dependency in step.depends_on
    )


def _source_operations(recipe: CapabilityRecipe) -> dict[str, tuple[str, ...]]:
    """Map each step to the api operations feeding it (transitive closure)."""
    api_by_step: dict[str, str] = {
        step.step_id: step.operation_id
        for step in recipe.steps
        if step.kind == "api" and step.operation_id is not None
    }
    by_step: dict[str, tuple[str, ...]] = {}
    for step in recipe.steps:
        ops = _upstream_api(step, recipe, api_by_step)
        by_step[step.step_id] = tuple(sorted(ops))
    return by_step


def _upstream_api(step: Any, recipe: CapabilityRecipe, api_by_step: dict[str, str]) -> set[str]:
    ops: set[str] = set()
    direct = api_by_step.get(step.step_id)
    if direct:
        ops.add(direct)
    by_id = {s.step_id: s for s in recipe.steps}
    for dependency in step.depends_on:
        dependency_step = by_id.get(dependency)
        if dependency_step is not None:
            ops |= _upstream_api(dependency_step, recipe, api_by_step)
    return ops


def _all_source_operations(
    recipe: CapabilityRecipe, fetches: dict[str, ResourceFetchResult]
) -> tuple[str, ...]:
    operations = {
        step.operation_id for step in recipe.steps if step.kind == "api" and step.operation_id
    }
    return tuple(sorted(operations))


def _first_footer(fetches: dict[str, ResourceFetchResult]) -> dict[str, str] | None:
    for fetch in fetches.values():
        if fetch.footer is not None:
            return fetch.footer
    return None


def _first_api_rows(fetches: dict[str, ResourceFetchResult]) -> tuple[dict[str, object], ...]:
    for fetch in fetches.values():
        if fetch.rows:
            return fetch.rows
    return ()


def _sum_field(rows: tuple[dict[str, object], ...], field: str) -> Decimal:
    return sum((_decimal_or_zero(row.get(field)) for row in rows), Decimal("0"))


def _decimal_or_zero(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _to_value(value: object, column: ResultColumnMeta) -> object:
    if value is None or value == UNAVAILABLE_VALUE:
        return value
    if column.column_type in ("money", "quantity") and isinstance(value, str):
        return _decimal_or_zero(value)
    if column.column_type in ("money", "quantity") and isinstance(value, Decimal):
        return value
    return value


def _build_totals(
    recipe: CapabilityRecipe,
    rows: tuple[dict[str, object], ...],
    fetches: dict[str, ResourceFetchResult],
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for column in recipe.result_columns:
        if column.column_type not in ("money", "quantity"):
            continue
        if rows:
            numeric = [
                Decimal(str(row.get(column.name)))
                for row in rows
                if row.get(column.name) is not None and row.get(column.name) != UNAVAILABLE_VALUE
            ]
            if numeric:
                totals[column.name] = sum(numeric, Decimal("0"))
    if not totals and fetches:
        # Detail recipe: totals come from the validated detail rows themselves.
        api_rows = _first_api_rows(fetches)
        je_total = _sum_field(api_rows, "je")
        if je_total != Decimal("0") or api_rows:
            totals["je"] = je_total
    return totals


__all__ = [
    "KernelCapabilityRunner",
    "KernelSettings",
    "StepExecutor",
    "render_table_from_run_result",
]
