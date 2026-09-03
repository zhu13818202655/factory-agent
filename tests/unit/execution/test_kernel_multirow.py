"""Multi-row kernel tests: multi-row computed tables, param-binding fan-out,
business-filter binding, unavailable columns, and the fan-out call budget.

Uses a fake step executor returning golden rows per operation so the recipe
DAG (including the FR-005/FR-009 chained progress chain) is exercised offline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import CapabilityId, DeptId, EmployeeId, TenantId, TimeRange
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.kernel import KernelCapabilityRunner, KernelSettings
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import UNAVAILABLE_VALUE, default_metric_registry
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest

_PLAN_ROWS: tuple[dict[str, Any], ...] = (
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

_SCLZD_ROWS: tuple[dict[str, Any], ...] = (
    {"id": "1001", "dh": "ZD-1", "dddh": "JH-1", "huohao": "HH001", "sssl": "13"},
    {"id": "1002", "dh": "ZD-2", "dddh": "JH-2", "huohao": "HH001", "sssl": "3"},
)

_PROGRESS_ROWS: dict[str, tuple[dict[str, Any], ...]] = {
    "1001": (
        {"userid": "1001", "worktype": "WT01", "name": "平车", "uid": "01001", "wsort": 1},
        {"userid": "1001", "worktype": "WT02", "name": "手工钉扣", "uid": "", "wsort": 2},
        {"userid": "1001", "worktype": "WT03", "name": "吊挂平车", "uid": "01001", "wsort": 3},
    ),
    "1002": (
        {"userid": "1002", "worktype": "WT01", "name": "平车", "uid": "01002", "wsort": 1},
        {"userid": "1002", "worktype": "WT02", "name": "手工钉扣", "uid": "", "wsort": 2},
        {"userid": "1002", "worktype": "WT03", "name": "吊挂平车", "uid": "", "wsort": 3},
    ),
}

_WSK_ROWS: tuple[dict[str, Any], ...] = (
    {"id": "1001", "huohao": "HH001", "worktype": "WT02", "sl": "93"},
)

_DEPT_ROWS: tuple[dict[str, Any], ...] = (
    {"id": "dept-a1", "name": "一车间"},
    {"id": "dept-a2", "name": "二车间"},
)


class FakeMultiRowExecutor:
    """Serves the golden rows per operation; WorktypeProgressQuery fans out by
    the bound userid parameter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.max_userid_calls: int | None = None

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> ResourceFetchResult:
        params = dict(extra_params or {})
        self.calls.append((request.operation_id, params))
        operation = request.operation_id
        rows: tuple[dict[str, Any], ...]
        if operation == "PlanGridPageList":
            rows = _PLAN_ROWS
        elif operation == "SclzdGridPageList":
            rows = _SCLZD_ROWS
        elif operation == "WorktypeProgressQuery":
            userid = params.get("userid")
            rows = _PROGRESS_ROWS.get(userid or "", ())
        elif operation == "WskQuery":
            rows = _WSK_ROWS
        elif operation == "BarcodeClQuery":
            rows = (
                {"dept": "dept-a1", "uid": "01001", "sssl": "4"},
                {"dept": "dept-a1", "uid": "01001", "sssl": "5"},
                {"dept": "dept-a2", "uid": "01002", "sssl": "3"},
            )
        elif operation == "HuohaoWtCLQuery":
            rows = ({"huohao": "HH001", "worktype": "WT01", "sssl": "12"},)
        elif operation == "DeptQuery":
            rows = _DEPT_ROWS
        elif operation == "GongziMxQuery":
            scheme = params.get("scheme", "")
            if scheme == "hz":
                rows = (
                    {"uid": "01001", "dept": "dept-a1", "je": "21.65", "sl": "20"},
                    {"uid": "01002", "dept": "dept-a2", "je": "3.75", "sl": "3"},
                )
            else:
                rows = ()
        elif operation == "GongziJeOrderQuery":
            rows = (
                {
                    "uid": "01001",
                    "uname": "模拟员工甲",
                    "dept": "dept-a1",
                    "bs": "5",
                    "je": "20.00",
                },
                {
                    "uid": "01002",
                    "uname": "模拟员工乙",
                    "dept": "dept-a2",
                    "bs": "1",
                    "je": "3.75",
                },
            )
        elif operation == "EmployeeQuery":
            rows = (
                {"uid": "01001", "uname": "模拟员工甲", "dept": "dept-a1"},
                {"uid": "01003", "uname": "模拟员工丙", "dept": "dept-a1"},
                {"uid": "01002", "uname": "模拟员工乙", "dept": "dept-a2"},
            )
        else:
            rows = ()
        return ResourceFetchResult(
            rows=tuple(rows),
            total=len(rows),
            pages_fetched=1,
            complete=True,
            footer=None,
        )


def _filters() -> NarrowedFilters:
    return NarrowedFilters(tenant_id=TenantId("APPKEY-A"), employee_ids=None, dept_ids=None)


def _range() -> TimeRange:
    return TimeRange(
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
    )


def _runner(executor: FakeMultiRowExecutor) -> KernelCapabilityRunner:
    return KernelCapabilityRunner(
        executor,
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
        clock=lambda: datetime(2026, 8, 21, 8, tzinfo=UTC),
        resource_columns=_resource_columns(),
    )


@pytest.mark.asyncio
async def test_fr005_progress_fans_out_over_materials() -> None:
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    # Fan-out called WorktypeProgressQuery once per material id.
    progress_calls = [p for op, p in executor.calls if op == "WorktypeProgressQuery"]
    assert sorted(p["userid"] for p in progress_calls) == ["1001", "1002"]
    assert result.api_call_count == 5  # plan + sclzd + 2x progress + wsk

    rows = {row[0]: row for row in result.rows}
    assert rows["PLAN-1"][4] == "0.6666666666666666"  # 2/3 done
    assert rows["PLAN-1"][5] is None  # all done -> no current worktype
    assert rows["PLAN-2"][4] == "0.3333333333333333"  # 1/3 done
    assert rows["PLAN-2"][5] == "手工钉扣"
    assert result.incomplete is False


@pytest.mark.asyncio
async def test_fr005_binds_order_filter_into_local_compute() -> None:
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                order_codes=frozenset({"PLAN-1"}),
            ),
            time_range=_range(),
        )
    )
    assert [row[0] for row in result.rows] == ["PLAN-1"]


@pytest.mark.asyncio
async def test_fr009_delivery_warning_uses_today_and_threshold() -> None:
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr009_factory_order_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    rows = {row[0]: row for row in result.rows}
    # Columns: order_code, huohao, customer_name, plan_qty, completed_qty,
    # progress_ratio, delivery_warning, days_remaining.
    # PLAN-1: finish 2026-07-31 already passed (today 2026-08-21), unfinished
    # (13 < 100) -> warning '1', days_remaining negative.
    assert rows["PLAN-1"][6] == "1"
    assert int(str(rows["PLAN-1"][7])) < 0
    # PLAN-2: finish 2026-09-30 is 40 days away, beyond the 3-day threshold
    # (总工期 29 天 -> max(1, ceil(2.9)) = 3) -> no warning.
    assert rows["PLAN-2"][6] == "0"
    assert int(str(rows["PLAN-2"][7])) == 40


@pytest.mark.asyncio
async def test_fr009_fanout_call_budget_exhausted_is_structured() -> None:
    executor = FakeMultiRowExecutor()
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
        settings=KernelSettings(max_api_calls=2),
        clock=lambda: datetime(2026, 8, 21, 8, tzinfo=UTC),
        resource_columns=_resource_columns(),
    )
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr009_factory_order_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.incomplete is True
    assert result.incomplete_reason == "pagination_call_budget_exhausted"
    # No covered material under a 2-call budget (plan+sclzd already used both):
    # the compute still runs against an empty progress table and reports
    # unavailable progress rather than fabricating a number.
    assert len(result.rows) >= 1
    assert all(row[5] == UNAVAILABLE_VALUE for row in result.rows)


def _resource_columns() -> dict[str, tuple[str, ...]]:
    from factory_agent.data_api.schemas import ROW_MODEL_BY_RESOURCE

    catalog = load_catalog()
    columns: dict[str, tuple[str, ...]] = {}
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        model = ROW_MODEL_BY_RESOURCE.get(operation.resource) if operation.resource else None
        columns[operation_id] = tuple(model.model_fields) if model else ()
    return columns


@pytest.mark.asyncio
async def test_unavailable_metric_columns_surface_sentinel_not_number() -> None:
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr007_workshop_output_comparison"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.incomplete is True
    # achievement_rate (C.9) is the sentinel in every row, never a number.
    for row in result.rows:
        assert row[5] == UNAVAILABLE_VALUE
    # per_capita and rank are real numbers.
    assert result.rows[0][1] == Decimal("9")  # dept-a1 total
    assert result.rows[0][4] == Decimal("1")  # rank


@pytest.mark.asyncio
async def test_fr011_groups_payroll_by_dept_with_confirmed_headcount() -> None:
    """FR-011 修订：在册人数来自 EmployeeQuery 全量，人均工资 = 应发合计 ÷ 在册.

    The fake executor returns two dept-a1 employees (01001, 01003) and one
    dept-a2 employee (01002) for EmployeeQuery, and je 20.00 / 3.75 for the
    two departments — so headcount and avg_wage are confirmed numbers, never
    the ``unavailable`` sentinel.
    """
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr011_factory_payroll_stats"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    by_dept = {row[0]: dict(zip(result.column_names, row)) for row in result.rows}
    assert set(by_dept) == {"一车间", "二车间"}
    a1 = by_dept["一车间"]
    assert a1["gross_total"] == Decimal("20.00")
    assert a1["headcount"] == Decimal("2")  # 在册人数 = EmployeeQuery 全员口径
    assert a1["avg_wage"] == Decimal("10.00")  # 20.00 ÷ 2
    a2 = by_dept["二车间"]
    assert a2["gross_total"] == Decimal("3.75")
    assert a2["headcount"] == Decimal("1")
    assert a2["avg_wage"] == Decimal("3.75")


@pytest.mark.asyncio
async def test_fr004_group_income_rank_locates_own_row_and_group() -> None:
    """FR-004 收入排名：MES 返回可见列表后按本人工号定位、按组过滤组内名次.

    Fake visible ranking: 01001 (dept-a1, 20.00) above 01002 (dept-a2, 3.75).
    The caller 01001 self_dept=dept-a1 → group_size counts dept-a1 peers only;
    only the caller's own result row is emitted.
    """
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr004_group_income_rank"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=frozenset({EmployeeId("01001")}),
                dept_ids=frozenset({DeptId("dept-a1")}),
            ),
            time_range=_range(),
        )
    )
    assert result.column_names == (
        "rank_position",
        "amount",
        "group_rank",
        "group_size",
    )
    assert len(result.rows) == 1  # 仅展示本人结果
    own = dict(zip(result.column_names, result.rows[0]))
    assert str(own["rank_position"]) == "1"
    assert Decimal(str(own["amount"])) == Decimal("20.00")
    assert str(own["group_rank"]) == "1"
    assert str(own["group_size"]) == "1"


@pytest.mark.asyncio
async def test_fr012_target_employee_recipe_runs() -> None:
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr012_employee_payroll"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=frozenset({EmployeeId("01001")}),
                dept_ids=None,
            ),
            time_range=_range(),
        )
    )
    # The recipe reuses the FR-002 summary path (scheme=hz). Uid injection is
    # adapter-side and covered by the integration slice + adapter tests.
    gongzi_calls = [p for op, p in executor.calls if op == "GongziMxQuery"]
    assert gongzi_calls
    assert all(p.get("scheme") == "hz" for p in gongzi_calls)
    assert result.rows[0][0] == Decimal("25.40")


@pytest.mark.asyncio
async def test_fr005_zero_total_worktype_degrades_to_unavailable() -> None:
    """A material with no worktype-progress rows must not fabricate a ratio."""

    class NoWorktypesExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, str] | None = None,
        ) -> ResourceFetchResult:
            if request.operation_id == "PlanGridPageList":
                return ResourceFetchResult(
                    rows=(
                        {
                            "dh": "PLAN-3",
                            "jhdh": "JH-3",
                            "khddh": "KHDD-3",
                            "huohao": "HH002",
                            "huohaoname": "模拟款B",
                            "zsl": "10",
                            "finish_date": "2026-09-30",
                            "dept": "dept-a1",
                        },
                    ),
                    total=1,
                    pages_fetched=1,
                    complete=True,
                )
            if request.operation_id == "SclzdGridPageList":
                return ResourceFetchResult(
                    rows=(
                        {
                            "id": "1003",
                            "dh": "ZD-3",
                            "dddh": "JH-3",
                            "huohao": "HH002",
                            "sssl": "7",
                        },
                    ),
                    total=1,
                    pages_fetched=1,
                    complete=True,
                )
            if request.operation_id == "WorktypeProgressQuery":
                return ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True)
            return await super().execute_full_step(
                filters, request, active_scope=active_scope, extra_params=extra_params
            )

    executor = NoWorktypesExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    # No scanned worktype -> progress_ratio is unavailable, current worktype None.
    assert len(result.rows) == 1
    assert result.rows[0][4] == UNAVAILABLE_VALUE
    assert result.rows[0][5] is None
