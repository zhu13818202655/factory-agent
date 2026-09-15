"""Multi-row kernel tests: multi-row computed tables, business-filter binding,
unavailable columns, and the progress/output chains.

Uses a fake step executor returning golden rows per operation so the reviewed
recipe DAGs (缝制进度族 + 已扫描产量族) are exercised offline.
"""



from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import CapabilityId, DeptId, EmployeeId, TenantId, TimeRange
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.kernel import KernelCapabilityRunner
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import UNAVAILABLE_VALUE, default_metric_registry
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest

#: 缝制生产进度流程卡行（ScjdQuery）：``bbreed`` 是款号，``wcl`` 是包完工率。
_SCJD_ROWS: tuple[dict[str, Any], ...] = (
    {
        "dh": "DH-1",
        "chuanghao": "1",
        "rq": "2026-07-01",
        "huohao": "HH001",
        "bbreed": "HH001",
        "description": "模拟款A",
        "zbs": "4",
        "zsl": "100",
        "sfwg": "0",
        "wcl": "50",
        "wts": "3",
    },
    {
        "dh": "DH-2",
        "chuanghao": "2",
        "rq": "2026-07-05",
        "huohao": "HH002",
        "bbreed": "HH002",
        "description": "模拟款B",
        "zbs": "2",
        "zsl": "40",
        "sfwg": "1",
        "wcl": "100",
        "wts": "5",
    },
)

#: 缝制生产进度详情（ScjdDetailQuery）：一行一个包，``wcs`` 是「总数/完成数」。
_SCJD_DETAIL_ROWS: tuple[dict[str, Any], ...] = (
    {
        "id": "1001",
        "baohao": "1",
        "color": "红",
        "chima": "S",
        "ganghao": "G1",
        "fhsl": "30",
        "wts": "3",
        "wcs": "90/45",
        "wcl": "50",
    },
    {
        "id": "1002",
        "baohao": "2",
        "color": "蓝",
        "chima": "M",
        "ganghao": "G2",
        "fhsl": "10",
        "wts": "3",
        "wcs": "30/30",
        "wcl": "100",
    },
)

#: 缝制工序进度（ScjdGxQuery）：只有已刷卡的工序，``uid`` 非空即视为该道已完成。
_SCJD_GX_ROWS: tuple[dict[str, Any], ...] = (
    {
        "userid": "1001",
        "worktype": "WT01",
        "name": "平车",
        "uid": "01001",
        "uname": "模拟员工甲",
        "dept": "dept-a1",
        "inputtime": "2026-07-02 08:00",
        "fhsl": "30",
        "zpsl": "30",
        "wsort": 1,
    },
    {
        "userid": "1001",
        "worktype": "WT02",
        "name": "手工钉扣",
        "uid": "",
        "uname": "",
        "dept": "dept-a1",
        "inputtime": "",
        "fhsl": "30",
        "zpsl": "0",
        "wsort": 2,
    },
)

#: 生产查询-已扫描（YskQuery，产量主源）：``huohao`` 是款号，``sl`` 是报工产量。
_YSK_ROWS: tuple[dict[str, Any], ...] = (
    {
        "inputtime": "2026-07-02 08:00",
        "uname": "模拟员工甲",
        "uid": "01001",
        "dept": "dept-a1",
        "id": "1001",
        "chuanghao": "1",
        "baohao": "1",
        "huohao": "HH001",
        "worktype": "裁剪",
        "fhsl": "9",
        "sl": "9",
        "je": "9",
    },
    {
        "inputtime": "2026-07-03 08:00",
        "uname": "模拟员工乙",
        "uid": "01002",
        "dept": "dept-a2",
        "id": "1002",
        "chuanghao": "2",
        "baohao": "1",
        "huohao": "HH002",
        "worktype": "裁剪",
        "fhsl": "3",
        "sl": "3",
        "je": "3",
    },
)

#: 生产制单（SclzdGridPageList）：产量挂订单的唯一桥（``Ysk.id`` → ``dh``）。
_SCLZD_ROWS: tuple[dict[str, Any], ...] = (
    {"id": "1001", "dh": "DH-1", "huohao": "HH001", "description": "模拟款A", "fhsl": "30"},
    {"id": "1002", "dh": "DH-2", "huohao": "HH002", "description": "模拟款B", "fhsl": "10"},
)

_DEPT_ROWS: tuple[dict[str, Any], ...] = (
    {"id": "dept-a1", "name": "一车间"},
    {"id": "dept-a2", "name": "二车间"},
)


class FakeMultiRowExecutor:
    """Serves the golden rows per operation for the reviewed progress/output recipes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

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
        if operation == "ScjdQuery":
            rows = _SCJD_ROWS
        elif operation == "ScjdDetailQuery":
            rows = _SCJD_DETAIL_ROWS
        elif operation == "ScjdGxQuery":
            rows = _SCJD_GX_ROWS
        elif operation == "YskQuery":
            rows = _YSK_ROWS
        elif operation == "SclzdGridPageList":
            rows = _SCLZD_ROWS
        elif operation == "DeptQuery":
            rows = _DEPT_ROWS
        elif operation == "GongziMxQuery":
            # Wage recipes fetch detail (scheme="") and aggregate locally; the
            # fake only serves the uid/dept/je/sl columns the aggregate needs.
            rows = (
                {"uid": "01001", "dept": "dept-a1", "je": "21.65", "sl": "20"},
                {"uid": "01002", "dept": "dept-a2", "je": "3.75", "sl": "3"},
            )
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
async def test_fr005_order_progress_reads_the_flowcard_list_in_one_call() -> None:
    """进度列表 = ScjdQuery 单次分页；不再逐单 fan-out 工序进度."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    # One paginated flow-card fetch; no per-order follow-up call.
    assert [op for op, _ in executor.calls] == ["ScjdQuery"]

    rows = {row[0]: row for row in result.rows}
    # Columns: order_code, style_code, product_name, bed_code, cut_date,
    # order_count, package_count, cut_qty, finished_packages, progress_ratio,
    # worktype_count, finish_state.
    assert rows["DH-1"][1] == "HH001"
    assert rows["DH-1"][5] == Decimal("1")  # 订单数
    assert rows["DH-1"][6] == Decimal("4")  # 包数 zbs
    assert rows["DH-1"][7] == Decimal("100")  # 裁剪数量 zsl
    assert rows["DH-1"][8] == Decimal("2")  # round(50/100 x 4) 完工包数
    assert rows["DH-1"][9] == Decimal("50")  # 包完工率 wcl，原值即进度
    assert rows["DH-1"][10] == Decimal("3")  # 工序数 wts
    assert rows["DH-1"][11] == "未完工"

    assert rows["DH-2"][9] == Decimal("100")
    assert rows["DH-2"][11] == "已完工"
    assert result.incomplete is False


@pytest.mark.asyncio
async def test_fr005_binds_order_and_style_filters_into_local_compute() -> None:
    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    by_order = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                order_codes=frozenset({"DH-1"}),
            ),
            time_range=_range(),
        )
    )
    assert [row[0] for row in by_order.rows] == ["DH-1"]

    # 只给款号时合并到款号粒度：一行一款号，进度按包数加权。
    by_style = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                style_codes=frozenset({"HH001"}),
            ),
            time_range=_range(),
        )
    )
    assert len(by_style.rows) == 1
    row = by_style.rows[0]
    assert row[0] is None  # 款号视图没有单一订单号
    assert row[1] == "HH001"
    assert row[5] == Decimal("1")  # 该款号下 1 个订单
    assert row[6] == Decimal("4")  # Σ包数
    assert row[8] == Decimal("2")  # Σ完工包数
    assert row[9] == Decimal("50")  # 2/4 加权后的包完工率


@pytest.mark.asyncio
async def test_fr009_order_overview_shares_the_progress_column_set() -> None:
    """全厂总览与订单进度用同一套列定义，且不再有交期族列."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr009_factory_order_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.column_names == (
        "order_code",
        "style_code",
        "product_name",
        "bed_code",
        "cut_date",
        "order_count",
        "package_count",
        "cut_qty",
        "finished_packages",
        "progress_ratio",
        "worktype_count",
        "finish_state",
    )
    assert {row[0] for row in result.rows} == {"DH-1", "DH-2"}
    assert result.incomplete is False


@pytest.mark.asyncio
async def test_fr005_package_detail_exposes_piece_worktype_units() -> None:
    """包级「总数/完成数」是件·工序口径，且物料编号可再下钻."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_package_detail"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                order_codes=frozenset({"DH-1"}),
            ),
            time_range=_range(),
        )
    )
    assert [op for op, _ in executor.calls] == ["ScjdDetailQuery"]
    # Columns: material_id, package_no, color, size, batch_no, cut_qty,
    # worktype_count, total_units, done_units, progress_ratio.
    first = result.rows[0]
    assert first[0] == "1001"
    assert first[5] == Decimal("30")  # fhsl
    assert first[7] == Decimal("90")  # wcs 前段 = 30 x 3
    assert first[8] == Decimal("45")  # wcs 后段
    assert first[9] == Decimal("50")  # wcl


@pytest.mark.asyncio
async def test_fr005_worktype_detail_lists_scanned_worktypes_only() -> None:
    """工序明细只列已刷卡工序，部门名由 DeptQuery 关联得到."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_worktype_detail"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                material_ids=frozenset({"1001"}),
            ),
            time_range=_range(),
        )
    )
    assert [op for op, _ in executor.calls] == ["ScjdGxQuery", "DeptQuery"]
    # Columns: worktype, worktype_order, is_done, issued_qty, done_qty,
    # operator, dept_name, input_time.
    assert [row[0] for row in result.rows] == ["平车", "手工钉扣"]
    assert result.rows[0][2] == "是"
    assert result.rows[1][2] == "否"
    assert result.rows[0][4] == Decimal("30")  # zpsl
    assert result.rows[0][6] == "一车间"  # dept dept-a1 -> DeptQuery.name
    assert result.rows[1][5] is None  # 未刷卡 -> 无操作人


@pytest.mark.asyncio
async def test_fr009_empty_window_yields_no_rows_and_no_fabrication() -> None:
    """空窗口是正常的空结果：不返回行，也不编造一个 0。

    调用预算（``KernelSettings.max_api_calls``）只约束 fan-out 步；进度族现在是
    单次列表拉取，因此不再有「预算耗尽」的运行时分支——fan-out 覆盖由
    ``test_kernel_fanout_concurrency.py`` 的探针 recipe 守护。
    """

    class VoidFlowcardExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, str] | None = None,
        ) -> ResourceFetchResult:
            if request.operation_id == "ScjdQuery":
                return ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True)
            return await super().execute_full_step(
                filters, request, active_scope=active_scope, extra_params=extra_params
            )

    executor = VoidFlowcardExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr009_factory_order_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.rows == ()
    assert result.incomplete is False


def _resource_columns() -> dict[str, tuple[str, ...]]:
    from factory_agent.data_api.schemas import ROW_MODEL_BY_RESOURCE

    catalog = load_catalog()
    columns: dict[str, tuple[str, ...]] = {}
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        model = ROW_MODEL_BY_RESOURCE.get(operation.resource) if operation.resource else None
        columns[operation_id] = tuple(model.model_fields) if model else ()
    return columns


_UNAVAILABLE_METRIC_RECIPE = """
version: 1
capabilities:
  - capability_id: probe_unavailable_metric
    title: 未确认口径探针
    required_slots: [time_range]
    steps:
      - step_id: fetch_depts
        kind: api
        operation_id: DeptQuery
      - step_id: compute
        kind: local
        depends_on: [fetch_depts]
        compute: |
          SELECT d.name AS dept_name, 'unavailable' AS gap_metric
          FROM fetch_depts d
    result_columns:
      - name: dept_name
        title: 车间/小组
        source_step: compute
      - name: gap_metric
        title: 未确认口径
        source_step: compute
        metric: plan_target_output
        column_type: percent
    metric_versions:
      plan_target_output: unavailable-target-v1
    degradation: incomplete_marker
"""


@pytest.mark.asyncio
async def test_fr007_ranks_depts_by_reported_output() -> None:
    """产量主源换成 YskQuery.sl（Σ 报工产量），且不再有达成率列."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr007_workshop_output_comparison"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.incomplete is False
    assert result.column_names == (
        "rank_position",
        "dept_name",
        "output_qty",
        "participant_count",
        "per_capita",
    )
    # 2 个可见部门 -> 给出名次，按报工产量降序。
    assert [row[1] for row in result.rows] == ["一车间", "二车间"]
    assert result.rows[0][0] == "1"
    assert result.rows[1][0] == "2"
    assert result.rows[0][2] == Decimal("9")  # Σ Ysk.sl (dept-a1)
    assert result.rows[1][2] == Decimal("3")  # Σ Ysk.sl (dept-a2)
    assert result.rows[0][3] == Decimal("1")  # COUNT(DISTINCT uid)
    assert result.rows[0][4] == Decimal("9")  # 人均产量


@pytest.mark.asyncio
async def test_fr007_single_visible_dept_reports_unavailable_rank() -> None:
    """可见部门数 = 1 时该列整体不可用，绝不伪造第 1 名."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr007_workshop_output_comparison"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                requested_dept_ids=frozenset({DeptId("dept-a1")}),
            ),
            time_range=_range(),
        )
    )
    assert len(result.rows) == 1
    assert result.rows[0][1] == "一车间"
    assert result.rows[0][0] == UNAVAILABLE_VALUE
    assert result.rows[0][2] == Decimal("9")


@pytest.mark.asyncio
async def test_fr010_expands_reported_output_by_style() -> None:
    """单部门/自范围视图：按「车间/小组 × 款号」展开报工产量."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr010_workshop_output_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.column_names == (
        "dept_name",
        "style_code",
        "output_qty",
        "participant_count",
        "per_capita",
    )
    assert [op for op, _ in executor.calls] == ["YskQuery", "DeptQuery"]
    assert [(row[0], row[1]) for row in result.rows] == [
        ("一车间", "HH001"),
        ("二车间", "HH002"),
    ]
    # 车间维度没有裁剪/计划数量来源，列定义里不得出现。
    assert "plan_qty" not in result.column_names
    assert "completed_qty" not in result.column_names


@pytest.mark.asyncio
async def test_fr006_output_attaches_orders_through_the_material_bridge() -> None:
    """产量明细按「订单×款号×工序」展开，订单号经 Sclzd.id -> dh 关联."""

    executor = FakeMultiRowExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr006_order_output"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert [op for op, _ in executor.calls] == ["YskQuery", "SclzdGridPageList", "ScjdQuery"]
    assert result.column_names == (
        "order_code",
        "style_code",
        "product_name",
        "worktype",
        "output_qty",
        "participant_count",
        "output_amount",
        "output_share",
    )
    # 按报工产量降序：DH-1 的 9 件在前。
    first = result.rows[0]
    assert first[0] == "DH-1"
    assert first[1] == "HH001"
    assert first[2] == "模拟款A"
    assert first[4] == Decimal("9")
    assert first[6] == Decimal("9")  # je 合计
    # 工序占比 = 该工序报工产量 ÷ 该单裁剪数量（ScjdQuery.zsl = 100）。
    # percent 列一旦带条件 unavailable 分支，整列就是 VARCHAR，数值以字符串返回
    # （与 fr007 名次列同法），由卡片层归一化展示。
    assert Decimal(str(first[7])) == Decimal("9")
    assert Decimal(str(result.rows[1][7])) == Decimal("7.5")


@pytest.mark.asyncio
async def test_unavailable_metric_columns_surface_sentinel_not_number(tmp_path: Path) -> None:
    """未确认口径的指标列一律渲染 unavailable 哨兵，绝不伪造数字.

    No shipped capability references such a metric any more, so a probe recipe
    keeps the kernel's fail-closed branch under test.
    """
    (tmp_path / "probe.yaml").write_text(_UNAVAILABLE_METRIC_RECIPE, encoding="utf-8")
    executor = FakeMultiRowExecutor()
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(load_catalog().operation_ids, tmp_path),
        default_metric_registry(),
        clock=lambda: datetime(2026, 8, 21, 8, tzinfo=UTC),
        resource_columns=_resource_columns(),
    )
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("probe_unavailable_metric"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.incomplete is True
    assert result.incomplete_reason == "metric_unavailable:plan_target_output"
    assert result.rows
    assert all(row[1] == UNAVAILABLE_VALUE for row in result.rows)


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
    # The recipe reuses the FR-002 fetch + local-aggregate path (scheme="");
    # the real-MES summary mode (hz) is defective and no recipe uses it.
    # Uid injection is adapter-side and covered by integration/adapter tests.
    gongzi_calls = [p for op, p in executor.calls if op == "GongziMxQuery"]
    assert gongzi_calls
    assert all(p.get("scheme") == "" for p in gongzi_calls)
    assert result.rows[0][0] == Decimal("25.40")


@pytest.mark.asyncio
async def test_fr005_zero_package_count_reports_zero_not_unavailable() -> None:
    """包数为 0 的流程卡不得除零，也不得伪造一个完成率."""

    class ZeroPackagesExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, str] | None = None,
        ) -> ResourceFetchResult:
            if request.operation_id == "ScjdQuery":
                return ResourceFetchResult(
                    rows=(
                        {
                            "dh": "DH-9",
                            "chuanghao": "9",
                            "rq": "2026-07-09",
                            "huohao": "HH009",
                            "bbreed": "HH009",
                            "description": "模拟款C",
                            "zbs": "0",
                            "zsl": "0",
                            "sfwg": "0",
                            "wcl": "",
                            "wts": "0",
                        },
                    ),
                    total=1,
                    pages_fetched=1,
                    complete=True,
                )
            return await super().execute_full_step(
                filters, request, active_scope=active_scope, extra_params=extra_params
            )

    executor = ZeroPackagesExecutor()
    runner = _runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                style_codes=frozenset({"HH009"}),
            ),
            time_range=_range(),
        )
    )
    # 款号粒度合并的分母是 Σ包数；Σ包数 = 0 时进度回落 0，既不除零也不报 unavailable。
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row[1] == "HH009"
    assert row[6] == Decimal("0")  # 包数
    assert row[8] == Decimal("0")  # 完工包数
    assert row[9] == Decimal("0")  # 进度
    assert row[11] == "未完工"
