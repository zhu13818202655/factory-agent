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

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from factory_agent.domain import CapabilityId, TimeRange
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.recipes import CapabilityRecipe, RecipeRegistry
from factory_agent.execution.result_table import (
    MetricRegistry,
    ResultColumnMeta,
    ResultTable,
)
from factory_agent.execution.sandbox_runtime import InteractionSandbox, SandboxTable
from factory_agent.ports.contracts import RenderColumn, RenderTable, ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest, CapabilityRunResult

_METADATA_TABLE = "_meta"

#: Reviewed filter params a recipe step may declare; they are never scope or
#: credential identifiers.
_FILTER_PARAM_KEYS = frozenset({"scheme", "Type", "Flag", "queryFooter"})


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


class KernelCapabilityRunner:
    """CapabilityRunner implementation backed by recipes, executor, and sandbox."""

    def __init__(
        self,
        executor: StepExecutor,
        recipes: RecipeRegistry,
        metrics: MetricRegistry,
        *,
        settings: KernelSettings | None = None,
    ) -> None:
        self._executor = executor
        self._recipes = recipes
        self._metrics = metrics
        self._settings = settings or KernelSettings()

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
            fetches = await self._fetch_api_steps(sandbox, recipe, filters, time_range)
            render_table = self._build_table(sandbox, recipe, fetches, time_range)
        result = self._to_run_result(render_table, time_range, fetches, started)
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
        sandbox.register_table(
            SandboxTable(
                name=_METADATA_TABLE,
                rows=({"days": days},),
                columns=(("days", "INTEGER"),),
            )
        )

    async def _fetch_api_steps(
        self,
        sandbox: InteractionSandbox,
        recipe: CapabilityRecipe,
        filters: Any,
        time_range: TimeRange,
    ) -> dict[str, ResourceFetchResult]:
        fetches: dict[str, ResourceFetchResult] = {}
        for step in recipe.steps:
            if step.kind != "api":
                continue
            if step.operation_id is None:
                raise InvalidRequestError(f"api step {step.step_id} has no operation")
            extra_params = self._reviewed_params(step.params)
            fetched = await self._executor.execute_full_step(
                filters,
                ExecutionRequest(
                    operation_id=step.operation_id,
                    time_range=(time_range.start, time_range.end),
                    pagination_size=self._settings.page_size,
                ),
                extra_params=extra_params,
            )
            if fetched.rows:
                columns = _table_columns(fetched.rows, recipe, step.step_id)
                sandbox.register_table(
                    SandboxTable(
                        name=step.step_id,
                        rows=tuple(cast(dict[str, Any], row) for row in fetched.rows),
                        columns=columns,
                    )
                )
            fetches[step.step_id] = fetched
        return fetches

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

        compute_outputs = self._run_compute_steps(sandbox, recipe, fetches)
        source_ops_by_step = _source_operations(recipe)
        warnings_by_column: dict[str, str] = {}

        column_metas: list[ResultColumnMeta] = []
        rendered_rows: list[dict[str, object]] = []

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

        if self._is_detail(recipe):
            rendered_rows = self._render_detail_rows(recipe, fetches)
        else:
            aggregate = self._aggregate_row(recipe, compute_outputs, fetches, time_range)
            rendered_rows = [aggregate] if aggregate is not None else []

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
    ) -> dict[str, dict[str, object]]:
        outputs: dict[str, dict[str, object]] = {}
        for step in recipe.steps:
            if step.kind != "local":
                continue
            if step.compute is None:
                raise InvalidRequestError(f"local step {step.step_id} has no compute")
            dependency_empty = _dependency_empty(step, fetches)
            if dependency_empty:
                outputs[step.step_id] = self._zero_aggregate(recipe, step.step_id)
                continue
            try:
                rows = sandbox.execute(step.compute)
            except InvalidRequestError as error:
                raise InvalidRequestError(
                    f"sandbox rejected local compute for {step.step_id}: {error}"
                ) from error
            outputs[step.step_id] = dict(zip(_compute_columns(recipe, step.step_id), rows[0]))
        return outputs

    def _zero_aggregate(self, recipe: CapabilityRecipe, step_id: str) -> dict[str, object]:
        columns = _compute_columns(recipe, step_id)
        return {column: _zero_for(column, recipe) for column in columns}

    def _aggregate_row(
        self,
        recipe: CapabilityRecipe,
        compute_outputs: dict[str, dict[str, object]],
        fetches: dict[str, ResourceFetchResult],
        time_range: TimeRange,
    ) -> dict[str, object] | None:
        for column in recipe.result_columns:
            if column.source_step in compute_outputs:
                output = compute_outputs[column.source_step]
                aggregate: dict[str, object] = {}
                for out_column in recipe.result_columns:
                    aggregate[out_column.name] = output.get(out_column.name)
                return aggregate
        return None

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
            api_call_count=sum(1 for _ in fetches),
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
    """A local step depends on an api step; if it fetched zero rows, skip SQL."""
    for dependency in step.depends_on:
        fetch = fetches.get(dependency)
        if fetch is not None and not fetch.rows:
            return True
    return False


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
    if value is None:
        return None
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
                if row.get(column.name) is not None
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
