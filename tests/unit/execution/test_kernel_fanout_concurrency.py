"""Fan-out concurrency: bounded batches must not change what a fan-out means.

The fan-out step issues one request per distinct bound-column value. Batching
those requests may only change how long the step takes — covered combinations,
row order, request count and the incompleteness reason must all stay exactly
what the serial walk produced, at every concurrency level.

The row shapes mirror the reviewed FR-005 recipe (plan → materials → one
progress lookup per material → workshop summary), so the batch behaviour is
exercised through the real kernel rather than a mocked step.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import CapabilityId, TenantId, TimeRange
from factory_agent.domain.errors import UpstreamUnavailableError
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.kernel import KernelCapabilityRunner, KernelSettings
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest

#: Material numbers the recipe fans its progress lookup over, one per order.
_MATERIALS = ("1001", "1002")

_PLAN_ROW_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "dh": "PLAN-1",
        "jhdh": "JH-1",
        "khddh": "KHDD-1",
        "huohao": "HH001",
        "huohaoname": "模拟款A",
        "khname": "客户甲",
        "zsl": "100",
        "ddsl": "100",
        "zhdate": "2026-07-01",
        "finish_date": "2026-07-31",
        "dept": "dept-a1",
    },
    {
        "dh": "PLAN-2",
        "jhdh": "JH-2",
        "khddh": "KHDD-2",
        "huohao": "HH001",
        "huohaoname": "模拟款A",
        "khname": "客户乙",
        "zsl": "50",
        "ddsl": "50",
        "zhdate": "2026-09-01",
        "finish_date": "2026-09-30",
        "dept": "dept-a2",
    },
)

#: Per-request latency long enough that unordered execution is observable,
#: short enough to keep the suite fast.
_LATENCY_SECONDS = 0.02


class LatencyExecutor:
    """Fake upstream with per-call latency, failure injection, and in-flight
    accounting, so a fan-out's concurrency is directly observable."""

    def __init__(
        self,
        *,
        fanout_latency: float = _LATENCY_SECONDS,
        fail_materials: frozenset[str] = frozenset(),
        incomplete_materials: frozenset[str] = frozenset(),
    ) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._fanout_latency = fanout_latency
        self._fail_materials = fail_materials
        self._incomplete_materials = incomplete_materials

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> ResourceFetchResult:
        params = dict(extra_params or {})
        self.calls.append((request.operation_id, params))
        if request.operation_id == "WorktypeProgressQuery":
            return await self._progress(params.get("userid", ""))
        rows = self._rows_for(request.operation_id)
        return ResourceFetchResult(
            rows=rows, total=len(rows), pages_fetched=1, complete=True, footer=None
        )

    async def _progress(self, material: str) -> ResourceFetchResult:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._fanout_latency)
            if material in self._fail_materials:
                raise UpstreamUnavailableError("fake upstream is down")
            rows: tuple[dict[str, Any], ...] = (
                {
                    "userid": material,
                    "worktype": "WT01",
                    "name": "平车",
                    "uid": f"0{material}",
                    "wsort": 1,
                },
                {"userid": material, "worktype": "WT02", "name": "手工钉扣", "uid": "", "wsort": 2},
            )
            complete = material not in self._incomplete_materials
            return ResourceFetchResult(
                rows=rows,
                total=len(rows),
                pages_fetched=1,
                complete=complete,
                reason=None if complete else "missing_pages",
                footer=None,
            )
        finally:
            self.in_flight -= 1

    def _rows_for(self, operation_id: str) -> tuple[dict[str, Any], ...]:
        if operation_id == "PlanGridPageList":
            return _PLAN_ROW_TEMPLATES
        if operation_id == "SclzdGridPageList":
            return tuple(
                {
                    "id": material,
                    "dh": f"ZD-{material}",
                    "dddh": f"JH-{index}",
                    "huohao": "HH001",
                    "sssl": "13",
                }
                for index, material in enumerate(_MATERIALS, start=1)
            )
        if operation_id == "WskQuery":
            return ({"id": "1001", "huohao": "HH001", "worktype": "WT02", "sl": "93"},)
        return ()


def _filters() -> NarrowedFilters:
    return NarrowedFilters(tenant_id=TenantId("APPKEY-A"), employee_ids=None, dept_ids=None)


def _range() -> TimeRange:
    return TimeRange(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 31, tzinfo=UTC))


async def _run(executor: LatencyExecutor, *, concurrency: int, max_api_calls: int = 500) -> Any:
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
        settings=KernelSettings(max_api_calls=max_api_calls, fanout_concurrency=concurrency),
        clock=lambda: datetime(2026, 8, 21, 8, tzinfo=UTC),
        resource_columns=_resource_columns(),
    )
    return await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=_filters(),
            time_range=_range(),
        )
    )


def _resource_columns() -> dict[str, tuple[str, ...]]:
    """Typed columns per operation, so an empty fetch still binds downstream."""
    from factory_agent.data_api.schemas import ROW_MODEL_BY_RESOURCE

    catalog = load_catalog()
    columns: dict[str, tuple[str, ...]] = {}
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        model = ROW_MODEL_BY_RESOURCE.get(operation.resource) if operation.resource else None
        columns[operation_id] = tuple(model.model_fields) if model else ()
    return columns


def _fanout_materials(executor: LatencyExecutor) -> list[str]:
    return [p["userid"] for op, p in executor.calls if op == "WorktypeProgressQuery"]


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2, 3, 8, 64])
async def test_every_concurrency_level_produces_the_serial_outcome(
    concurrency: int,
) -> None:
    """Batching may only change how long the step takes, never what it means."""
    serial_executor = LatencyExecutor()
    serial = await _run(serial_executor, concurrency=1)
    # plan + sclzd + one progress request per material + wsk
    assert serial.api_call_count == 3 + len(_MATERIALS)
    assert serial.incomplete is False
    assert serial.incomplete_reason is None

    concurrent_executor = LatencyExecutor()
    concurrent = await _run(concurrent_executor, concurrency=concurrency)

    assert concurrent.rows == serial.rows
    assert concurrent.column_names == serial.column_names
    assert concurrent.api_call_count == serial.api_call_count
    assert concurrent.incomplete is serial.incomplete
    assert concurrent.incomplete_reason == serial.incomplete_reason
    assert _fanout_materials(concurrent_executor) == _fanout_materials(serial_executor)


@pytest.mark.asyncio
async def test_a_batch_never_exceeds_the_configured_concurrency() -> None:
    serial = LatencyExecutor()
    await _run(serial, concurrency=1)
    assert serial.max_in_flight == 1

    parallel = LatencyExecutor()
    await _run(parallel, concurrency=2)
    assert parallel.max_in_flight == 2

    # Wider than the number of combinations: batching caps at what exists,
    # never at the configured ceiling.
    wide = LatencyExecutor()
    await _run(wide, concurrency=64)
    assert wide.max_in_flight == len(_MATERIALS)


@pytest.mark.asyncio
async def test_the_budget_covers_whole_combinations_and_reports_the_rest() -> None:
    """plan+sclzd spend two calls, so a 3-call budget covers one material.

    The budget bounds the fan-out only; the remaining recipe steps still run,
    and the uncovered material stays an explicit ``unavailable``.
    """
    for concurrency in (1, 4):
        executor = LatencyExecutor()
        result = await _run(executor, concurrency=concurrency, max_api_calls=3)

        assert _fanout_materials(executor) == [_MATERIALS[0]]
        assert result.api_call_count == 4  # plan + sclzd + 1 progress + wsk
        assert result.incomplete is True
        assert result.incomplete_reason == "pagination_call_budget_exhausted"
        assert "unavailable" in {str(row[-1]) for row in result.rows}, (
            "the uncovered material must stay an explicit unavailable, never a number"
        )


@pytest.mark.asyncio
async def test_a_zero_budget_issues_no_fanout_request_at_all() -> None:
    """plan+sclzd already spend the whole budget: nothing may be sent."""
    for concurrency in (1, 4):
        executor = LatencyExecutor()
        result = await _run(executor, concurrency=concurrency, max_api_calls=2)

        assert _fanout_materials(executor) == []
        assert result.incomplete is True
        assert result.incomplete_reason == "pagination_call_budget_exhausted"


@pytest.mark.asyncio
async def test_the_budget_verdict_still_wins_over_an_earlier_pagination_reason() -> None:
    """Reason precedence must not depend on how the batches were packed."""
    serial_executor = LatencyExecutor(incomplete_materials=frozenset({_MATERIALS[0]}))
    serial = await _run(serial_executor, concurrency=1, max_api_calls=3)

    concurrent_executor = LatencyExecutor(incomplete_materials=frozenset({_MATERIALS[0]}))
    concurrent = await _run(concurrent_executor, concurrency=4, max_api_calls=3)

    assert serial.incomplete_reason == "pagination_call_budget_exhausted"
    assert concurrent.incomplete_reason == serial.incomplete_reason
    assert concurrent.rows == serial.rows


@pytest.mark.asyncio
async def test_an_incomplete_pagination_still_marks_the_result_incomplete() -> None:
    """Without budget pressure the fetch's own reason is what surfaces."""
    serial = await _run(
        LatencyExecutor(incomplete_materials=frozenset({_MATERIALS[0]})), concurrency=1
    )
    concurrent = await _run(
        LatencyExecutor(incomplete_materials=frozenset({_MATERIALS[0]})), concurrency=4
    )

    assert serial.incomplete is True
    assert serial.incomplete_reason == "pagination_missing_pages"
    assert concurrent.incomplete_reason == serial.incomplete_reason
    assert concurrent.rows == serial.rows


@pytest.mark.asyncio
async def test_an_upstream_failure_degrades_the_step_identically() -> None:
    for concurrency in (1, 4):
        result = await _run(
            LatencyExecutor(fail_materials=frozenset({_MATERIALS[1]})),
            concurrency=concurrency,
        )

        assert result.incomplete is True
        assert result.incomplete_reason == "upstream_unavailable"


@pytest.mark.asyncio
async def test_a_degraded_fetch_registers_a_typed_table_for_downstream_compute() -> None:
    """An empty fan-out must still leave the local compute a bindable table."""
    for concurrency in (1, 4):
        result = await _run(
            LatencyExecutor(fail_materials=frozenset(_MATERIALS)), concurrency=concurrency
        )

        assert result.incomplete is True
        assert result.incomplete_reason == "upstream_unavailable"
        # The recipe still renders its plan rows, with the progress metric in
        # the explicit unavailable state rather than a fabricated number.
        assert result.rows
        assert "unavailable" in {str(row[-1]) for row in result.rows}


@pytest.mark.asyncio
async def test_no_request_outlives_a_failed_batch() -> None:
    """Every issued request is awaited before the step gives up."""
    executor = LatencyExecutor(fail_materials=frozenset({_MATERIALS[0]}))
    await _run(executor, concurrency=4)

    assert executor.in_flight == 0


@pytest.mark.asyncio
async def test_result_row_order_is_reproducible() -> None:
    """The fan-out walk order decides result row order, so it may not be a set scan.

    A plainly ordered ``SELECT DISTINCT`` in DuckDB returns rows in an unstable
    order, which made the same plan produce different result row orders between
    runs. Asserted through the runner rather than the private SQL builder.
    """
    first = await _run(LatencyExecutor(), concurrency=4)
    second = await _run(LatencyExecutor(), concurrency=4)

    assert len(first.rows) > 1
    assert list(first.rows) == list(second.rows)


def test_plan_rows_are_stable_fixtures() -> None:
    """Guards the fixture this file reads, so a silent edit cannot mask a bug."""
    assert [row["dh"] for row in _PLAN_ROW_TEMPLATES] == ["PLAN-1", "PLAN-2"]
    assert Decimal("100") == Decimal(_PLAN_ROW_TEMPLATES[0]["zsl"])
