from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import CapabilityId, DeptId, EmployeeId, TenantId, TimeRange
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.kernel import KernelCapabilityRunner, StepExecutor
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import UNAVAILABLE_VALUE, default_metric_registry
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest
from tests.support.payload import as_dict, as_list

#: 生产进度 V2（ScjdQuery）列表层行集：粒度 = 制单 × 部门（方案 M7）。
#: 订单维度使用 jhdh；制单日期使用接口独立返回的 rq。
#: 发卡数量 fhsl / 实收数量 sssl 是可加度量，行级 wcl 仅作参考（列表按 Σ 重算）。
_SCJD_ROWS: tuple[dict[str, Any], ...] = (
    {
        "dh": "20260701001",
        "rq": "2026-07-01",
        "jhdh": "DH-1",
        "chuanghao": "1",
        "huohao": "HH001",
        "bbreed": "HH001",
        "dept": "dept-a1",
        "deptname": "一车间",
        "bs": "4",
        "fhsl": "100",
        "sssl": "50",
        "wts": "3",
        "wcl": "50",
    },
    {
        "dh": "20260705001",
        "rq": "2026-07-01",
        "jhdh": "DH-2",
        "chuanghao": "2",
        "huohao": "HH002",
        "bbreed": "HH002",
        "dept": "dept-a2",
        "deptname": "二车间",
        "bs": "2",
        "fhsl": "40",
        "sssl": "40",
        "wts": "5",
        "wcl": "100",
    },
)

#: 包级下钻行集：粒度 = dh × baohao × userid × color × chima（方案 C.2）。
_SCJD_PACKAGE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "dh": "20260701001",
        "rq": "2026-07-01",
        "jhdh": "DH-1",
        "baohao": "1",
        "userid": "1001",
        "color": "红",
        "chima": "S",
        "fhsl": "30",
        "sssl": "15",
        "wts": "3",
        "wgs": "1",
        "wcl": "50",
    },
    {
        "dh": "20260705001",
        "rq": "2026-07-01",
        "jhdh": "DH-2",
        "baohao": "2",
        "userid": "1002",
        "color": "蓝",
        "chima": "M",
        "fhsl": "10",
        "sssl": "10",
        "wts": "3",
        "wgs": "3",
        "wcl": "100",
    },
)

#: 工序级下钻行集：粒度 = userid × uid × ... × wsort（方案 C.2）。
#: uid 为空 = 未刷卡的待做工序行（V2 契约行为，附录 A.4）。
_SCJD_WORKTYPE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "userid": "1001",
        "worktype": "WT01",
        "wtname": "平车",
        "uid": "01001",
        "uname": "模拟员工甲",
        "dept": "dept-a1",
        "deptname": "一车间",
        "fhsl": "30",
        "zpsl": "30",
        "wsort": "1",
    },
    {
        "userid": "1001",
        "worktype": "WT02",
        "wtname": "手工钉扣",
        "uid": "",
        "uname": "",
        "dept": "dept-a1",
        "deptname": "一车间",
        "fhsl": "30",
        "zpsl": "0",
        "wsort": "2",
    },
)

#: 工资产量 V2（GongziClQuery）订单粒度行集（FR-006）：
#: jhdh × chuanghao × huohao × bbreed × worktype × wtname × uid（方案 C.3）。
_GONGZI_CL_ORDER_ROWS: tuple[dict[str, Any], ...] = (
    {
        "jhdh": "DH-1",
        "chuanghao": "1",
        "huohao": "HH001",
        "bbreed": "HH001",
        "worktype": "裁剪",
        "wtname": "裁剪",
        "uid": "01001",
        "sl": "9",
        "je": "9",
    },
    {
        "jhdh": "DH-2",
        "chuanghao": "2",
        "huohao": "HH002",
        "bbreed": "HH002",
        "worktype": "裁剪",
        "wtname": "裁剪",
        "uid": "01002",
        "sl": "3",
        "je": "3",
    },
)

#: 工资产量 V2 部门粒度行集（FR-007/010；带 inputdate 亦服务 FR-013 日序列）。
_GONGZI_CL_DEPT_ROWS: tuple[dict[str, Any], ...] = (
    {
        "inputdate": "2026-07-02",
        "dept": "dept-a1",
        "deptname": "一车间",
        "bbreed": "HH001",
        "uid": "01001",
        "sl": "9",
        "je": "9",
    },
    {
        "inputdate": "2026-07-03",
        "dept": "dept-a2",
        "deptname": "二车间",
        "bbreed": "HH002",
        "uid": "01002",
        "sl": "3",
        "je": "3",
    },
)

#: 工资产量 V2 明细模式行集（FR-002/003/004/008/011/012 共用底料：
#: 不传 groupbyFields = 工资族预取粒度；人员/部门聚合在配方本地 compute 完成）。
#: 01001（dept-a1）je 12.40+7.60=20.00、bs 3+2=5；01002（dept-a2）je 3.75、bs 1。
_GONGZI_CL_DETAIL_ROWS: tuple[dict[str, Any], ...] = (
    {
        "inputdate": "2026-07-02",
        "uid": "01001",
        "uname": "模拟员工甲",
        "dept": "dept-a1",
        "deptname": "一车间",
        "jhdh": "DH-1",
        "chuanghao": "1",
        "bbreed": "HH001",
        "worktype": "WT01",
        "huohao": "HH001",
        "wtname": "裁剪",
        "sl": "20",
        "price": "1.0825",
        "je": "12.40",
        "bs": "3",
    },
    {
        "inputdate": "2026-07-02",
        "uid": "01001",
        "uname": "模拟员工甲",
        "dept": "dept-a1",
        "deptname": "一车间",
        "jhdh": "DH-1",
        "chuanghao": "1",
        "bbreed": "HH001",
        "worktype": "WT01",
        "huohao": "HH001",
        "wtname": "锁边",
        "sl": "8",
        "price": "0.95",
        "je": "7.60",
        "bs": "2",
    },
    {
        "inputdate": "2026-07-03",
        "uid": "01002",
        "uname": "模拟员工乙",
        "dept": "dept-a2",
        "deptname": "二车间",
        "jhdh": "DH-2",
        "chuanghao": "2",
        "bbreed": "HH002",
        "worktype": "WT02",
        "huohao": "HH002",
        "wtname": "裁剪",
        "sl": "3",
        "price": "1.25",
        "je": "3.75",
        "bs": "1",
    },
)

_DEPT_ROWS: tuple[dict[str, Any], ...] = (
    {"id": "dept-a1", "name": "一车间"},
    {"id": "dept-a2", "name": "二车间"},
)


def _group(params: dict[str, Any]) -> tuple[str, ...]:
    group: Any = params.get("groupbyFields")
    if isinstance(group, (list, tuple)):
        items = cast("list[Any] | tuple[Any, ...]", group)
        return tuple(str(item) for item in items)
    if isinstance(group, str) and group:
        return (group,)
    return ()


def _resource_by_operation() -> dict[str, str]:
    catalog = load_catalog()
    return {
        operation_id: catalog.get(operation_id).resource or ""
        for operation_id in catalog.operation_ids
    }


_RESOURCE_BY_OPERATION = _resource_by_operation()


class FakeMultiRowExecutor:
    """Serves the golden rows per operation + ``groupbyFields`` grain.

    Rows are validated + serialized through the V2 row models exactly like the
    real adapter path, so every fetch table carries the full schema column set
    (缺列的行集在真实链路里不会出现).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ResourceFetchResult:
        from factory_agent.data_api.schemas import ROW_MODEL_BY_RESOURCE

        params = dict(extra_params or {})
        self.calls.append((request.operation_id, params))
        operation = request.operation_id
        rows: tuple[dict[str, Any], ...]
        if operation == "ScjdQuery":
            group = _group(params)
            if "baohao" in group:
                rows = _SCJD_PACKAGE_ROWS
            elif "wsort" in group:
                rows = _SCJD_WORKTYPE_ROWS
            else:
                rows = _SCJD_ROWS
        elif operation == "GongziClQuery":
            group = _group(params)
            if not group:
                # 明细模式 = FR-002/003/004/008/011/012 的统一底料（预取粒度）。
                rows = _GONGZI_CL_DETAIL_ROWS
            elif "jhdh" in group:
                rows = _GONGZI_CL_ORDER_ROWS
            else:
                # inputdate / dept×bbreed 分组：FR-007/010/013 的部门×款号粒度。
                rows = _GONGZI_CL_DEPT_ROWS
        elif operation == "DeptQuery":
            rows = _DEPT_ROWS
        elif operation == "EmployeeQuery":
            rows = (
                {"uid": "01001", "uname": "模拟员工甲", "dept": "dept-a1"},
                {"uid": "01003", "uname": "模拟员工丙", "dept": "dept-a1"},
                {"uid": "01002", "uname": "模拟员工乙", "dept": "dept-a2"},
            )
        else:
            rows = ()
        resource = _RESOURCE_BY_OPERATION.get(operation)
        model = ROW_MODEL_BY_RESOURCE.get(resource) if resource else None
        # 只对 V2 动态分组行（gongzi_cl / scjd_v2）做 model_dump 序列化（生产
        # 路径行为，行集带全字段）；EmployeeQuery/DeptQuery 等基础数据模型字段
        # 必填，fake 行保持原样。
        if model is not None and rows and resource in {"gongzi_cl", "scjd_v2"}:
            rows = tuple(dict(model.model_validate(row).model_dump()) for row in rows)
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


def make_multirow_runner(executor: StepExecutor) -> KernelCapabilityRunner:
    return KernelCapabilityRunner(
        executor,
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
        clock=lambda: datetime(2026, 8, 21, 8, tzinfo=UTC),
        resource_columns=_resource_columns(),
    )


@pytest.mark.asyncio
async def test_fr005_order_progress_reads_the_flowcard_list_in_one_call() -> None:
    """进度列表 = ScjdQuery V2 单次 M7 拉取；订单号取 jhdh，进度按 Σ实收/Σ发卡重算."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr005_order_progress"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    # One M7 flow-card fetch; no per-order follow-up call.
    assert [op for op, _ in executor.calls] == ["ScjdQuery"]

    rows = {row[0]: dict(zip(result.column_names, row, strict=True)) for row in result.rows}
    assert rows["DH-1"]["plan_code"] == "DH-1"
    assert rows["DH-1"]["plan_missing"] is False
    assert rows["DH-1"]["order_count"] == Decimal("1")
    assert rows["DH-1"]["sheet_count"] == Decimal("1")
    assert rows["DH-1"]["package_count"] == Decimal("4")
    assert rows["DH-1"]["issued_qty"] == Decimal("100")
    assert rows["DH-1"]["received_qty"] == Decimal("50")
    assert Decimal(str(rows["DH-1"]["progress_ratio"])) == Decimal("50")
    assert rows["DH-1"]["worktype_count"] == Decimal("3")
    assert rows["DH-1"]["unfinished_sheet_count"] == Decimal("1")
    assert rows["DH-1"]["finish_state"] == "未完工"
    assert Decimal(str(rows["DH-2"]["progress_ratio"])) == Decimal("100")
    assert rows["DH-2"]["finished_sheet_count"] == Decimal("1")
    assert rows["DH-2"]["finish_state"] == "全部完工"
    detail = result.detail_tables[0]
    assert detail.rows[0]["sheet_code"] == "20260701001"
    assert detail.rows[0]["created_date"] == "2026-07-01"
    assert detail.rows[0]["style_name"] == "HH001"
    assert result.incomplete is False


@pytest.mark.asyncio
async def test_fr005_binds_order_and_style_filters_into_local_compute() -> None:
    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
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

    # 款号仅筛选参与计划号汇总的数据，进度按 Σ实收/Σ发卡 重算。
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
    row = dict(zip(by_style.column_names, by_style.rows[0], strict=True))
    assert row["order_code"] == "DH-1"
    assert row["order_count"] == Decimal("1")
    assert row["sheet_count"] == Decimal("1")
    assert row["package_count"] == Decimal("4")
    assert Decimal(str(row["progress_ratio"])) == Decimal("50")
    assert len(by_style.detail_tables[0].rows) == 1
    assert by_style.detail_tables[0].rows[0]["style_name"] == "HH001"


@pytest.mark.asyncio
async def test_fr009_order_overview_shares_the_progress_column_set() -> None:
    """全厂总览与订单进度用同一套列定义（V2 列：发卡/实收口径，无交期族列）."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr009_factory_order_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.column_names == (
        "order_code",
        "plan_code",
        "plan_missing",
        "order_count",
        "sheet_count",
        "package_count",
        "issued_qty",
        "received_qty",
        "progress_ratio",
        "progress_label",
        "worktype_count",
        "finished_sheet_count",
        "unfinished_sheet_count",
        "unavailable_sheet_count",
        "finish_state",
    )
    assert {row[0] for row in result.rows} == {"DH-1", "DH-2"}
    assert result.incomplete is False


@pytest.mark.parametrize("capability", ["fr005_order_progress", "fr009_factory_order_overview"])
@pytest.mark.asyncio
async def test_progress_orders_merge_sheets_by_plan_and_keep_one_unlinked_group(
    capability: str,
) -> None:
    class PlanExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, Any] | None = None,
        ) -> ResourceFetchResult:
            from factory_agent.data_api.schemas import ScjdV2Row

            self.calls.append((request.operation_id, dict(extra_params or {})))
            base = _SCJD_ROWS[0]
            overrides: tuple[dict[str, Any], ...] = (
                {"jhdh": ""},
                {"jhdh": "", "dept": "dept-a2", "fhsl": "20", "sssl": "20", "bs": "0", "wts": "1"},
                {
                    "jhdh": None,
                    "dh": "20260702001",
                    "rq": "2026-07-01",
                    "bbreed": "HH002",
                    "chuanghao": "2",
                    "fhsl": "80",
                    "sssl": "0",
                    "bs": "2",
                    "wts": "5",
                },
                {
                    "jhdh": " ",
                    "dh": "20260703001",
                    "rq": "2026-07-01",
                    "bbreed": "HH003",
                    "chuanghao": "3",
                    "fhsl": "0",
                    "sssl": "0",
                    "bs": "0",
                    "wts": "0",
                },
                {
                    "jhdh": "PLAN-A",
                    "dh": "20260704001",
                    "rq": "2026-07-01",
                    "chuanghao": "4",
                    "fhsl": "10",
                    "sssl": "10",
                    "bs": "1",
                    "wts": "1",
                },
                {
                    "jhdh": "PLAN-A",
                    "dh": "20260705001",
                    "rq": "2026-07-01",
                    "chuanghao": "4",
                    "fhsl": "30",
                    "sssl": "0",
                    "bs": "3",
                    "wts": "2",
                },
                {
                    "jhdh": "未关联计划号",
                    "dh": "20260706001",
                    "rq": "2026-07-01",
                    "bbreed": "HH004",
                    "chuanghao": "5",
                    "fhsl": "5",
                    "sssl": "5",
                    "bs": "1",
                    "wts": "1",
                },
                {
                    "jhdh": " PLAN-A ",
                    "dh": "20260707001",
                    "rq": "2026-07-01",
                    "fhsl": "15",
                    "sssl": "0",
                    "bs": "1",
                    "wts": "1",
                },
            )
            rows = tuple(
                dict(ScjdV2Row.model_validate({**base, **row}).model_dump()) for row in overrides
            )
            return ResourceFetchResult(rows=rows, total=len(rows), pages_fetched=1, complete=True)

    executor = PlanExecutor()
    result = await make_multirow_runner(executor).run(
        CapabilityRunRequest(
            capability_id=CapabilityId(capability), filters=_filters(), time_range=_range()
        )
    )
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.rows]
    assert len(rows) == 4
    unlinked = next(
        row
        for row in rows
        if row["order_code"] == "未关联计划号" and row["issued_qty"] == Decimal("200")
    )
    assert unlinked["order_count"] == Decimal("1")
    assert unlinked["sheet_count"] == Decimal("3")
    assert unlinked["package_count"] == Decimal("6")
    assert Decimal(str(unlinked["progress_ratio"])) == Decimal("35")
    assert unlinked["worktype_count"] == Decimal("9")
    assert unlinked["finish_state"] == "未完工"
    assert unlinked["plan_code"] is None
    assert unlinked["plan_missing"] is True
    assert unlinked["finished_sheet_count"] == Decimal("0")
    assert unlinked["unfinished_sheet_count"] == Decimal("2")
    assert unlinked["unavailable_sheet_count"] == Decimal("1")
    plan = next(row for row in rows if row["order_code"] == "PLAN-A")
    assert plan["order_count"] == Decimal("1")
    assert plan["sheet_count"] == Decimal("2")
    assert plan["plan_missing"] is False
    assert plan["finished_sheet_count"] == Decimal("1")
    assert plan["unfinished_sheet_count"] == Decimal("1")
    assert Decimal(str(plan["progress_ratio"])) == Decimal("25")
    literal = next(row for row in rows if row["issued_qty"] == Decimal("5"))
    assert literal["order_code"] == "未关联计划号"
    assert literal["plan_code"] == "未关联计划号"
    assert literal["plan_missing"] is False
    assert literal["finish_state"] == "全部完工"
    details = result.detail_tables[0].rows
    assert len(details) == 7
    assert sum(row["plan_code"] is None for row in details) == 3
    assert sum(row["plan_code"] == "未关联计划号" for row in details) == 1
    assert any(row["order_code"] == " PLAN-A " for row in rows)
    assert result.totals["order_count"] == Decimal("4")
    assert result.totals["sheet_count"] == Decimal("7")
    assert result.totals["package_count"] == Decimal("12")
    assert result.totals["issued_qty"] == Decimal("260")
    assert result.incomplete is False
    assert [op for op, _ in executor.calls] == ["ScjdQuery"]
    filtered = await make_multirow_runner(executor).run(
        CapabilityRunRequest(
            capability_id=CapabilityId(capability),
            filters=NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                order_codes=frozenset({" PLAN-A "}),
            ),
            time_range=_range(),
        )
    )
    assert len(filtered.rows) == 1
    assert filtered.rows[0][0] == " PLAN-A "


@pytest.mark.parametrize("capability", ["fr005_order_progress", "fr009_factory_order_overview"])
@pytest.mark.asyncio
async def test_progress_plan_does_not_present_partial_dimensions_as_complete(
    capability: str,
) -> None:
    class MissingDimensionExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, Any] | None = None,
        ) -> ResourceFetchResult:
            from factory_agent.data_api.schemas import ScjdV2Row

            self.calls.append((request.operation_id, dict(extra_params or {})))
            base = _SCJD_ROWS[0]
            rows = tuple(
                dict(ScjdV2Row.model_validate(row).model_dump())
                for row in (
                    base,
                    {**base, "dept": "dept-a2", "bbreed": None, "chuanghao": None},
                )
            )
            return ResourceFetchResult(rows=rows, total=len(rows), pages_fetched=1, complete=True)

    executor = MissingDimensionExecutor()
    result = await make_multirow_runner(executor).run(
        CapabilityRunRequest(
            capability_id=CapabilityId(capability), filters=_filters(), time_range=_range()
        )
    )
    assert len(result.rows) == 1
    row = dict(zip(result.column_names, result.rows[0], strict=True))
    assert row["sheet_count"] == Decimal("1")
    assert row["issued_qty"] == Decimal("200")
    assert result.incomplete is False
    assert [op for op, _ in executor.calls] == ["ScjdQuery"]
    detail = result.detail_tables[0].rows[0]
    assert detail["sheet_code"] == "20260701001"
    assert detail["created_date"] == "2026-07-01"
    assert detail["style_name"] is None
    assert detail["bed_code"] is None


@pytest.mark.asyncio
async def test_fr005_package_detail_exposes_piece_worktype_units() -> None:
    """包级明细 = ScjdQuery V2 包粒度拉取；完成率 = 实收 ÷ 发卡."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
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
    assert [op for op, _ in executor.calls] == ["ScjdQuery"]
    # Columns: material_id, package_no, color, size, issued_qty, received_qty,
    # worktype_count, done_worktype_count, progress_ratio.
    first = result.rows[0]
    assert first[0] == "1001"
    assert first[4] == Decimal("30")  # 发卡 fhsl
    assert first[5] == Decimal("15")  # 实收 sssl
    assert first[7] == Decimal("1")  # 已完工工序数 wgs
    assert Decimal(str(first[8])) == Decimal("50")  # wcl = 15/30 × 100


@pytest.mark.asyncio
async def test_fr005_worktype_detail_lists_scanned_worktypes_only() -> None:
    """工序明细 = ScjdQuery V2 工序粒度拉取；部门名由行集 deptname 直出（无 DeptQuery）."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
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
    assert [op for op, _ in executor.calls] == ["ScjdQuery"]
    # Columns: worktype, worktype_order, is_done, issued_qty, done_qty,
    # operator, dept_name.
    assert [row[0] for row in result.rows] == ["平车", "手工钉扣"]
    assert result.rows[0][2] == "是"  # uid 非空 = 已刷卡
    assert result.rows[1][2] == "否"
    assert result.rows[0][3] == Decimal("30")  # fhsl
    assert result.rows[0][4] == Decimal("30")  # zpsl
    assert result.rows[0][6] == "一车间"  # deptname 直出
    assert result.rows[1][5] is None  # 未刷卡 -> 无操作人


@pytest.mark.asyncio
async def test_fr009_empty_window_yields_no_rows_and_no_fabrication() -> None:
    """空窗口是正常的空结果：不返回行，也不编造一个 0。"""

    class VoidFlowcardExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, Any] | None = None,
        ) -> ResourceFetchResult:
            if request.operation_id == "ScjdQuery":
                return ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True)
            return await super().execute_full_step(
                filters, request, active_scope=active_scope, extra_params=extra_params
            )

    executor = VoidFlowcardExecutor()
    runner = make_multirow_runner(executor)
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
    """产量主源 = GongziClQuery V2 明细的本地部门聚合，部门名行集直出."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
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
        "dept_id",
        "dept_name",
        "output_qty",
        "participant_count",
        "per_capita",
    )
    # 2 个可见部门 -> 给出名次，按报工产量降序、dept_id 升序打破并列（G.5）。
    assert [row[2] for row in result.rows] == ["一车间", "二车间"]
    assert result.rows[0][0] == "1"
    assert result.rows[1][0] == "2"
    assert result.rows[0][3] == Decimal("28")  # Σ sl (dept-a1)
    assert result.rows[1][3] == Decimal("3")  # Σ sl (dept-a2)
    assert result.rows[0][4] == Decimal("1")  # COUNT(DISTINCT uid)
    assert result.rows[0][5] == Decimal("28")  # 人均产量


@pytest.mark.asyncio
async def test_fr007_single_visible_dept_reports_unavailable_rank() -> None:
    """可见部门数 = 1 时该列整体不可用，绝不伪造第 1 名."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
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
    assert result.rows[0][2] == "一车间"
    assert result.rows[0][0] == UNAVAILABLE_VALUE
    assert result.rows[0][3] == Decimal("28")


@pytest.mark.asyncio
async def test_fr010_expands_reported_output_by_style() -> None:
    """单部门/自范围视图：按「车间/小组 × 款号」展开报工产量（GongziClQuery 单调用）."""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr010_workshop_output_overview"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.column_names == (
        "dept_name",
        "dept_id",
        "style_code",
        "output_qty",
        "participant_count",
        "per_capita",
    )
    assert [op for op, _ in executor.calls] == ["GongziClQuery"]
    assert [(row[0], row[1], row[2]) for row in result.rows] == [
        ("一车间", "dept-a1", "HH001"),
        ("二车间", "dept-a2", "HH002"),
    ]
    # 车间维度没有裁剪/计划数量来源，列定义里不得出现。
    assert "plan_qty" not in result.column_names
    assert "completed_qty" not in result.column_names


@pytest.mark.asyncio
async def test_fr006_output_reads_orders_from_jhdh_without_material_bridge() -> None:
    """主结果按计划号归组，工序分解使用同范围的完整明细表。"""

    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr006_order_output"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert [op for op, _ in executor.calls] == ["GongziClQuery", "ScjdQuery"]
    assert result.column_names == (
        "order_code",
        "plan_code",
        "plan_missing",
        "style_count",
        "output_qty",
        "participant_count",
        "output_amount",
    )
    first = dict(zip(result.column_names, result.rows[0], strict=True))
    assert first["order_code"] == "DH-1"
    assert first["plan_code"] == "DH-1"
    assert first["style_count"] == Decimal("1")
    assert first["output_qty"] == Decimal("28")
    assert first["participant_count"] == Decimal("1")
    assert first["output_amount"] == Decimal("20.00")
    assert result.totals["participant_count"] == Decimal("2")
    assert result.column_units is not None
    assert result.column_units["output_qty"] == "件·工序"
    details = result.detail_tables[0].rows
    assert len(details) == 3
    assert details[0]["style_name"] == "HH001"
    assert details[0]["worktype"] == "裁剪"
    assert Decimal(str(details[0]["output_share"])) == Decimal("20")
    assert Decimal(str(details[1]["output_share"])) == Decimal("8")
    assert Decimal(str(details[2]["output_share"])) == Decimal("7.5")


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
    """FR-011：工资 = 明细模式底料 + 本地按部门聚合，在册人数来自 EmployeeQuery 全量.

    The fake executor returns two dept-a1 employees (01001, 01003) and one
    dept-a2 employee (01002) for EmployeeQuery, and locally aggregated je
    20.00 / 3.75 for the two departments — so headcount and avg_wage are
    confirmed numbers, never the ``unavailable`` sentinel.
    """
    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr011_factory_payroll_stats"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    by_dept = {row[1]: dict(zip(result.column_names, row)) for row in result.rows}
    assert set(by_dept) == {"一车间", "二车间"}
    a1 = by_dept["一车间"]
    assert a1["dept_id"] == "dept-a1"
    assert a1["gross_total"] == Decimal("20.00")
    assert a1["headcount"] == Decimal("2")  # 在册人数 = EmployeeQuery 全员口径
    assert a1["avg_wage"] == Decimal("10.00")  # 20.00 ÷ 2
    a2 = by_dept["二车间"]
    assert a2["gross_total"] == Decimal("3.75")
    assert a2["headcount"] == Decimal("1")
    assert a2["avg_wage"] == Decimal("3.75")
    # 全厂级单值 KPI 走 aux 输出（卡片专属，不进主表）。
    assert result.card is not None
    kpis = {item["label"]: item for item in as_list(result.card["metrics"])}
    assert kpis["工资总额"]["value"] == "23.75"
    assert kpis["在册人数"]["value"] == "3"
    assert kpis["人均工资"]["value"] == "7.92"  # 23.75 ÷ 3，保留两位


@pytest.mark.asyncio
async def test_fr004_group_income_rank_locates_own_row_and_group() -> None:
    """FR-004 收入排名：明细模式底料 + 本地按人聚合，位次 = 本地排序（je 降序）.

    Fake detail rows aggregate to: 01001 (dept-a1, je 20.00) above
    01002 (dept-a2, 3.75). The caller 01001 self_dept=dept-a1 → group_size
    counts dept-a1 peers only; only the caller's own result row is emitted.
    """
    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
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
    runner = make_multirow_runner(executor)
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
    # The recipe reuses the FR-002 fetch + local-aggregate path (明细模式,
    # 不传 groupbyFields = 工资族预取粒度)。uid 注入由适配层完成，
    # 覆盖于 integration/adapter 测试。fake 底料全量 SUM(je) = 23.75。
    gongzi_calls = [p for op, p in executor.calls if op == "GongziClQuery"]
    assert gongzi_calls
    assert all(p.get("Flag") == "0" for p in gongzi_calls)
    assert all("groupbyFields" not in p for p in gongzi_calls)
    assert result.rows[0][0] == Decimal("23.75")


@pytest.mark.asyncio
async def test_fr005_zero_package_count_reports_zero_not_unavailable() -> None:
    """发卡数量为 0 的制单不得除零，也不得伪造一个完成率."""

    class ZeroPackagesExecutor(FakeMultiRowExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, Any] | None = None,
        ) -> ResourceFetchResult:
            if request.operation_id == "ScjdQuery":
                return ResourceFetchResult(
                    rows=(
                        {
                            "dh": "20260709001",
                            "rq": "2026-07-01",
                            "jhdh": "DH-9",
                            "chuanghao": "9",
                            "huohao": "HH009",
                            "bbreed": "HH009",
                            "dept": "dept-a1",
                            "deptname": "一车间",
                            "bs": "0",
                            "fhsl": "0",
                            "sssl": "0",
                            "wts": "0",
                            "wcl": "",
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
    runner = make_multirow_runner(executor)
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
    # 分母是 Σ发卡；Σ发卡 = 0 时进度回落 0，既不除零也不报 unavailable。
    assert len(result.rows) == 1
    row = dict(zip(result.column_names, result.rows[0], strict=True))
    assert row["order_code"] == "DH-9"
    assert row["sheet_count"] == Decimal("1")
    assert row["package_count"] == Decimal("0")
    assert row["issued_qty"] == Decimal("0")
    assert Decimal(str(row["progress_ratio"])) == Decimal("0")
    assert row["progress_label"] == "暂无发卡"
    assert row["worktype_count"] == Decimal("0")
    assert row["unavailable_sheet_count"] == Decimal("1")
    assert row["finish_state"] == "无法判断"


@pytest.mark.asyncio
async def test_fr013_dashboard_combines_ranking_kpis_and_daily_chart() -> None:
    """FR-013 综合看板：主表车间排名 + aux KPI（总产量/在产订单/人均月产）+ 日序列 chart.

    Fake 数据：GongziClQuery 部门粒度两车间报工（dept-a1 sl=28 @07-02、
    dept-a2 sl=3 @07-03）、EmployeeQuery 在册 3 人、ScjdQuery M7 中
    DH-1（Σ实收 50 < Σ发卡 100）未完工。
    """
    executor = FakeMultiRowExecutor()
    runner = make_multirow_runner(executor)
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr013_factory_output_dashboard"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.rows
    by_dept = {row[2]: dict(zip(result.column_names, row)) for row in result.rows}
    assert set(by_dept) == {"一车间", "二车间"}
    a1 = by_dept["一车间"]
    assert a1["rank_position"] == "1"
    assert a1["output_qty"] == Decimal("28")
    a2 = by_dept["二车间"]
    assert a2["rank_position"] == "2"
    assert a2["output_qty"] == Decimal("3")

    card = result.card
    assert card is not None
    # aux KPI（D-2 口径 + T1 换代：Σ实收 < Σ发卡 的制单去重 = DH-1 一单）。
    kpis = {item["label"]: item for item in as_list(card["metrics"])}
    assert kpis["总产量"]["value"] == "31"
    assert kpis["在产制单"]["value"] == "1"
    assert kpis["人均月产"]["value"] == "10"
    # 日序列 chart：按 inputdate 归日，62 点内保持日粒度；标签由卡片层归一化为 MM-DD。
    chart = as_dict(card["chart"])
    assert chart["type"] == "bar"
    assert chart["unit"] == "件·工序"
    assert chart["granularity"] == "day"
    assert chart["points"] == [
        {"label": "07-02", "value": "28"},
        {"label": "07-03", "value": "3"},
    ]
    # 卡片级 actions：点车间 → fr010 产量明细。
    assert card["actions"] == [
        {
            "type": "drill",
            "label": "查看该车间/小组的产量明细",
            "capability_id": "fr010_workshop_output_overview",
            "bind_from": {"dept_ids": "dept_id"},
        }
    ]
