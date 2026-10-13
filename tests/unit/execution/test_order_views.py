from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.progress_detail import aggregate_progress_rows
from factory_agent.data_api.result_cache import ResultCacheExecutor, ResultCacheMode
from factory_agent.data_api.schemas import GongziClRow, ScjdV2Row
from factory_agent.domain import CapabilityId, DeptId, EmployeeId, Role, TenantId, TimeRange
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.order_views import OrderViewSelection
from factory_agent.ports.session import CapabilityRunRequest
from tests.unit.data_api.test_result_cache import PREFETCH_WINDOW, MemoryStore, make_result_cache
from tests.unit.execution.test_kernel_multirow import FakeMultiRowExecutor, make_multirow_runner


class OrderViewExecutor(FakeMultiRowExecutor):
    def __init__(
        self,
        progress: tuple[dict[str, Any], ...] = (),
        output: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__()
        self.progress = progress
        self.output = output

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ResourceFetchResult:
        self.calls.append((request.operation_id, dict(extra_params or {})))
        if request.operation_id == "ScjdQuery":
            rows = tuple(ScjdV2Row.model_validate(row).model_dump() for row in self.progress)
        else:
            rows = tuple(GongziClRow.model_validate(row).model_dump() for row in self.output)
        returned_rows: tuple[dict[str, object], ...] = tuple(dict(row) for row in rows)
        return ResourceFetchResult(
            rows=returned_rows, total=len(rows), pages_fetched=1, complete=True
        )


def _request(capability: str, style: str | None = None) -> CapabilityRunRequest:
    return CapabilityRunRequest(
        capability_id=CapabilityId(capability),
        filters=NarrowedFilters(
            tenant_id=TenantId("SYNTHETIC-TENANT"),
            employee_ids=None,
            dept_ids=None,
            style_codes=frozenset({style}) if style else None,
        ),
        time_range=TimeRange(
            start=datetime(2026, 9, 1, tzinfo=UTC),
            end=datetime(2026, 10, 1, tzinfo=UTC),
        ),
    )


@pytest.mark.parametrize("capability", ["fr005_order_progress", "fr009_factory_order_overview"])
@pytest.mark.asyncio
async def test_progress_states_keep_unfinished_sheets_when_other_sheets_overreceive(
    capability: str,
) -> None:
    base: dict[str, Any] = {
        "dh": "202609010001",
        "rq": "2026-09-05",
        "jhdh": None,
        "huohao": "1250",
        "bbreed": "T5401",
        "chuanghao": "17",
        "dept": "dept-a",
        "bs": "1",
        "fhsl": "100",
        "sssl": "110",
        "wts": "1",
    }
    executor = OrderViewExecutor(
        progress=(
            {**base, "fhsl": "20", "sssl": "22"},
            {**base, "jhdh": "", "dept": "dept-b", "fhsl": "80", "sssl": "88"},
            {**base, "jhdh": " ", "dh": "202609020001", "sssl": "90"},
            {**base, "dh": "202609030001", "fhsl": "0", "sssl": "0"},
            {**base, "jhdh": "ROUND", "dh": "202609040001", "fhsl": "100000", "sssl": "99999"},
            {**base, "jhdh": "未关联计划号", "dh": "202609050001", "sssl": "100"},
        )
    )
    result = await make_multirow_runner(executor).run(_request(capability))
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.rows]
    unlinked = next(row for row in rows if row["plan_missing"])
    assert unlinked["sheet_count"] == Decimal("3")
    assert unlinked["finished_sheet_count"] == Decimal("1")
    assert unlinked["unfinished_sheet_count"] == Decimal("1")
    assert unlinked["unavailable_sheet_count"] == Decimal("1")
    assert Decimal(str(unlinked["progress_ratio"])) == Decimal("100")
    assert Decimal(str(unlinked["progress_label"]).split("%", 1)[0]) == Decimal("100")
    assert str(unlinked["progress_label"]).endswith("%（仍有未完工）")
    assert unlinked["finish_state"] == "未完工"
    rounded = next(row for row in rows if row["plan_code"] == "ROUND")
    assert Decimal(str(rounded["progress_ratio"])) == Decimal("100")
    assert rounded["progress_label"] == "接近100%（仍有未完工）"
    assert rounded["finish_state"] == "未完工"
    literal = next(row for row in rows if row["plan_code"] == "未关联计划号")
    assert literal["plan_missing"] is False
    assert literal["finish_state"] == "全部完工"
    assert all(
        name not in result.column_names for name in ("sheet_code", "bed_code", "created_date")
    )
    details = result.detail_tables[0].rows
    assert len(details) == 5
    merged = next(row for row in details if row["sheet_code"] == "202609010001")
    assert Decimal(str(merged["issued_qty"])) == Decimal("100")
    assert Decimal(str(merged["received_qty"])) == Decimal("110")
    assert merged["style_code"] == "1250"
    assert merged["style_name"] == "T5401"
    assert merged["style_label"] == "T5401"
    assert merged["created_date"] == "2026-09-05"
    assert merged["finish_state"] == "已完工"
    assert [operation for operation, _ in executor.calls] == ["ScjdQuery"]


@pytest.mark.asyncio
async def test_missing_sheet_key_keeps_contributions_without_counting_a_real_sheet() -> None:
    executor = OrderViewExecutor(
        progress=(
            {
                "jhdh": None,
                "dh": None,
                "rq": "2026-09-05",
                "bbreed": "T5401",
                "fhsl": "10",
                "sssl": "10",
                "bs": "1",
                "wts": "1",
            },
        )
    )
    result = await make_multirow_runner(executor).run(_request("fr009_factory_order_overview"))
    row = dict(zip(result.column_names, result.rows[0], strict=True))
    assert row["sheet_count"] == Decimal("0")
    assert row["issued_qty"] == Decimal("10")
    assert row["finished_sheet_count"] == Decimal("0")
    assert row["unavailable_sheet_count"] == Decimal("0")
    assert row["finish_state"] == "无法判断"
    assert result.detail_tables[0].rows[0]["sheet_code"] is None


@pytest.mark.asyncio
async def test_output_main_groups_and_independent_participants_keep_real_style_keys() -> None:
    base: dict[str, Any] = {
        "jhdh": None,
        "huohao": "1250",
        "bbreed": "T5401",
        "worktype": "WT1",
        "wtname": "缝制",
        "uid": "u1",
        "sl": "10",
        "je": "1",
    }
    executor = OrderViewExecutor(
        output=(
            base,
            {**base, "jhdh": "", "worktype": "WT2", "sl": "20", "je": "2"},
            {**base, "jhdh": " ", "uid": "u2", "sl": "5", "je": "0.5"},
            {**base, "huohao": "1251", "uid": "u2", "sl": "7", "je": "0.7"},
            {**base, "jhdh": "PLAN", "sl": "2", "je": "0.2"},
            {**base, "jhdh": "未关联计划号", "uid": "u3", "sl": "3", "je": "0.3"},
            {**base, "bbreed": "OTHER", "uid": "outside-style", "sl": "1000", "je": "100"},
        ),
        progress=(
            {
                "jhdh": "未关联计划号",
                "dh": "202609010001",
                "rq": "2026-09-05",
                "bbreed": "T5401",
                "huohao": "1250",
                "fhsl": "10",
            },
        ),
    )
    result = await make_multirow_runner(executor).run(_request("fr006_order_output", "T5401"))
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.rows]
    assert len(rows) == 3
    unlinked = next(row for row in rows if row["plan_missing"])
    assert unlinked["output_qty"] == Decimal("42")
    assert unlinked["output_amount"] == Decimal("4.2")
    assert unlinked["style_count"] == Decimal("2")
    assert unlinked["participant_count"] == Decimal("2")
    assert result.totals["participant_count"] == Decimal("3")
    assert result.totals["output_qty"] == Decimal("47")
    assert result.column_units is not None
    assert result.column_units["output_qty"] == "件·工序"
    details = result.detail_tables[0].rows
    assert len(details) == 5
    unlinked_details = [row for row in details if row["plan_missing"]]
    assert {row["worktype_code"] for row in unlinked_details} == {"WT1", "WT2"}
    assert {row["style_code"] for row in unlinked_details} == {"1250", "1251"}
    assert all(row["style_label"] == "T5401" for row in details)
    assert all(row["output_share"] == "unavailable" for row in unlinked_details)
    literal_detail = next(row for row in details if row["plan_code"] == "未关联计划号")
    assert Decimal(str(literal_detail["output_share"])) == Decimal("30")
    assert [operation for operation, _ in executor.calls] == ["GongziClQuery", "ScjdQuery"]


@pytest.mark.parametrize("capability", ["fr006_order_output", "fr009_factory_order_overview"])
@pytest.mark.parametrize("plan", [None, "未关联计划号", " PLAN "])
@pytest.mark.asyncio
async def test_group_selection_narrows_summary_details_and_nonadditive_totals(
    capability: str, plan: str | None
) -> None:
    plans = (None, "未关联计划号", " PLAN ")
    executor = OrderViewExecutor(
        output=tuple(
            {
                "jhdh": key,
                "huohao": "1250",
                "bbreed": "T5401",
                "uid": f"employee-{index}",
                "worktype": "WT1",
                "sl": str(index * 10),
                "je": str(index),
            }
            for index, key in enumerate(plans, 1)
        ),
        progress=tuple(
            {
                "jhdh": key,
                "dh": f"2026090{index}0001",
                "rq": "2026-09-05",
                "huohao": "1250",
                "bbreed": "T5401",
                "fhsl": str(index * 10),
                "sssl": str(index * 5),
                "bs": str(index),
                "wts": "1",
            }
            for index, key in enumerate(plans, 1)
        ),
    )
    view = OrderViewSelection(
        view_id="worktype_details" if capability == "fr006_order_output" else "sheet_details",
        group_kind="unlinked" if plan is None else "plan",
        plan_code=plan,
    )
    request = replace(_request(capability), order_view=view)
    result = await make_multirow_runner(executor).run(request)
    assert len(result.rows) == 1
    row = dict(zip(result.column_names, result.rows[0], strict=True))
    assert row["plan_code"] == plan
    assert row["plan_missing"] is (plan is None)
    quantity = Decimal((plans.index(plan) + 1) * 10)
    if capability == "fr006_order_output":
        assert result.totals["output_qty"] == quantity
        assert result.totals["participant_count"] == Decimal("1")
    else:
        assert result.totals["issued_qty"] == quantity
        assert row["sheet_count"] == Decimal("1")
    assert len(result.detail_tables[0].rows) == 1
    assert result.detail_tables[0].rows[0]["plan_code"] == plan


class RangeProjectionSource(FakeMultiRowExecutor):
    def __init__(self) -> None:
        super().__init__()
        wage = {
            "inputdate": "2026-09-05",
            "jhdh": "P-A",
            "dept": "dept-a",
            "deptname": "合成车间甲",
            "uid": "u1",
            "huohao": "1250",
            "bbreed": "T5401",
            "worktype": "W1",
            "wtname": "缝制",
            "sl": "10",
            "je": "1",
        }
        progress = {
            "dh": "202609050001",
            "rq": "2026-09-05",
            "jhdh": "P-A",
            "dept": "dept-a",
            "deptname": "合成车间甲",
            "uid": "u1",
            "uname": "合成人员甲",
            "huohao": "1250",
            "bbreed": "T5401",
            "worktype": "W1",
            "wtname": "缝制",
            "baohao": "1",
            "userid": "M1",
            "wsort": "1",
            "bs": "1",
            "fhsl": "100",
            "sssl": "50",
            "zpsl": "50",
            "wgs": "1",
            "wts": "2",
        }
        self.wages = tuple(
            GongziClRow.model_validate(row).model_dump()
            for row in (
                wage,
                {**wage, "dept": "dept-b", "uid": "u2", "sl": "20", "je": "2"},
                {**wage, "jhdh": "P-B", "uid": "u3", "sl": "99", "je": "9.9"},
            )
        )
        self.progress = tuple(
            ScjdV2Row.model_validate(row).model_dump()
            for row in (
                progress,
                {
                    **progress,
                    "dh": "202609050002",
                    "rq": "2026-09-05",
                    "dept": "dept-b",
                    "uid": "u2",
                    "userid": "M2",
                    "fhsl": "200",
                    "sssl": "80",
                },
                {
                    **progress,
                    "dh": "202609050003",
                    "rq": "2026-09-05",
                    "jhdh": "P-B",
                    "uid": "u3",
                    "userid": "M3",
                    "fhsl": "990",
                    "sssl": "990",
                },
            )
        )
        self.received_filters: list[Any] = []

    def validate_step(self, filters: Any, request: Any, active_scope: Any = None) -> None:
        pass

    async def execute_full_step(
        self,
        filters: Any,
        request: Any,
        active_scope: Any = None,
        extra_params: Any = None,
    ) -> ResourceFetchResult:
        params = dict(extra_params or {})
        self.calls.append((request.operation_id, params))
        self.received_filters.append(filters)
        rows = self.wages
        if request.operation_id == "ScjdQuery":
            rows = (
                aggregate_progress_rows(self.progress, params["groupbyFields"])
                if params.get("groupbyFields")
                else self.progress
            )
        returned_rows: tuple[dict[str, object], ...] = tuple(dict(row) for row in rows)
        return ResourceFetchResult(
            rows=returned_rows, total=len(rows), pages_fetched=1, complete=True
        )


@pytest.mark.parametrize(
    "mode", [ResultCacheMode.OFF, ResultCacheMode.READ, ResultCacheMode.SHADOW]
)
@pytest.mark.parametrize(
    "role,explicit_dept,quantity,issued",
    [
        (Role.MANAGER, False, "10", "100"),
        (Role.OWNER, False, "30", "300"),
        (Role.OWNER, True, "10", "100"),
    ],
)
@pytest.mark.asyncio
async def test_kernel_native_and_cached_order_views_obey_plan_and_effective_department(
    mode: ResultCacheMode, role: Role, explicit_dept: bool, quantity: str, issued: str
) -> None:
    source = RangeProjectionSource()
    cache = make_result_cache(mode=mode, store=MemoryStore())
    if mode.reads:
        assert await cache.write(
            TenantId("SYNTHETIC-TENANT"),
            "GongziClQuery",
            source.wages,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            ("2026-09-01", "2026-09-30"),
            source="precompute",
        )
        assert await cache.write(
            TenantId("SYNTHETIC-TENANT"),
            "ScjdQuery",
            source.progress,
            None,
            {},
            ("2026-09-01", "2026-09-30"),
            source="precompute",
        )
    filters = NarrowedFilters(
        tenant_id=TenantId("SYNTHETIC-TENANT"),
        employee_ids=None,
        dept_ids=frozenset({DeptId("dept-a")}),
        plan_codes=frozenset({"P-A"}),
        requested_dept_ids=frozenset({DeptId("dept-a")}) if explicit_dept else None,
    )
    runner = make_multirow_runner(ResultCacheExecutor(source, cache))
    request = replace(_request("fr006_order_output"), filters=filters, role=role)
    output = await runner.run(request)
    assert output.incomplete is False
    assert len(output.rows) == 1
    assert output.totals["output_qty"] == Decimal(quantity)
    assert all(row["plan_code"] == "P-A" for row in output.detail_tables[0].rows)
    progress = await runner.run(
        replace(request, capability_id=CapabilityId("fr005_order_progress"))
    )
    assert progress.incomplete is False
    assert len(progress.rows) == 1
    assert progress.totals["issued_qty"] == Decimal(issued)
    assert all(row["plan_code"] == "P-A" for row in progress.detail_tables[0].rows)
    assert output.api_call_count == (0 if mode is ResultCacheMode.READ else 2)
    assert progress.api_call_count == (0 if mode is ResultCacheMode.READ else 1)
    if mode is ResultCacheMode.SHADOW:
        assert output.cache_source is not None
        assert progress.cache_source is not None
    if mode is not ResultCacheMode.READ:
        assert all(params.get("groupbyFields") for op, params in source.calls if op == "ScjdQuery")


@pytest.mark.parametrize(
    "mode", [ResultCacheMode.OFF, ResultCacheMode.READ, ResultCacheMode.SHADOW]
)
@pytest.mark.asyncio
async def test_kernel_employee_output_keeps_scope_and_marks_order_denominator_unavailable(
    mode: ResultCacheMode,
) -> None:
    source = RangeProjectionSource()
    cache = make_result_cache(mode=mode, store=MemoryStore())
    if mode.reads:
        assert await cache.write(
            TenantId("SYNTHETIC-TENANT"),
            "GongziClQuery",
            source.wages,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            ("2026-09-01", "2026-09-30"),
            source="precompute",
        )
        assert await cache.write(
            TenantId("SYNTHETIC-TENANT"),
            "ScjdQuery",
            source.progress,
            None,
            {},
            ("2026-09-01", "2026-09-30"),
            source="precompute",
        )
    filters = NarrowedFilters(
        tenant_id=TenantId("SYNTHETIC-TENANT"),
        employee_ids=frozenset({EmployeeId("u1")}),
        dept_ids=frozenset({DeptId("dept-a")}),
        plan_codes=frozenset({"P-A"}),
    )
    result = await make_multirow_runner(ResultCacheExecutor(source, cache)).run(
        replace(_request("fr006_order_output"), filters=filters, role=Role.MANAGER)
    )
    assert result.incomplete is False
    assert result.totals["output_qty"] == Decimal("10")
    assert result.totals["participant_count"] == Decimal("1")
    assert all(row["output_share"] == "unavailable" for row in result.detail_tables[0].rows)
    assert all(value.employee_ids == filters.employee_ids for value in source.received_filters)
    assert all("groupbyFields" not in params for _, params in source.calls)


@pytest.mark.parametrize(
    "mode", [ResultCacheMode.OFF, ResultCacheMode.READ, ResultCacheMode.SHADOW]
)
@pytest.mark.asyncio
async def test_progress_package_scope_is_filtered_before_losing_department_dimensions(
    mode: ResultCacheMode,
) -> None:
    source = RangeProjectionSource()
    cache = make_result_cache(mode=mode, store=MemoryStore())
    if mode.reads:
        assert await cache.write(
            TenantId("SYNTHETIC-TENANT"),
            "ScjdQuery",
            source.progress,
            None,
            {},
            ("2026-09-01", "2026-09-30"),
            source="precompute",
        )
    filters = NarrowedFilters(
        tenant_id=TenantId("SYNTHETIC-TENANT"),
        employee_ids=None,
        dept_ids=frozenset({DeptId("dept-a")}),
        order_codes=frozenset({"202609050001"}),
        plan_codes=frozenset({"P-A"}),
        style_codes=frozenset({"T5401"}),
        material_ids=frozenset({"M1"}),
    )
    result = await make_multirow_runner(ResultCacheExecutor(source, cache)).run(
        replace(_request("fr005_order_package_detail"), filters=filters, role=Role.MANAGER)
    )
    assert result.incomplete is False
    assert len(result.rows) == 1
    row = dict(zip(result.column_names, result.rows[0], strict=True))
    assert row["material_id"] == "M1"
    assert row["issued_qty"] == Decimal("100")
    for (_, params), sent_filters in zip(source.calls, source.received_filters, strict=True):
        assert "groupbyFields" not in params
        assert params["dh"] == "202609050001"
        assert "userid" not in params
        assert sent_filters.material_ids is None
        assert sent_filters.order_codes is None


@pytest.mark.parametrize(
    "mode", [ResultCacheMode.OFF, ResultCacheMode.READ, ResultCacheMode.SHADOW]
)
@pytest.mark.asyncio
async def test_kernel_progress_date_and_window_are_driven_by_rq_without_sheet_parsing(
    mode: ResultCacheMode,
) -> None:
    source = RangeProjectionSource()
    base = source.progress[0]
    source.progress = (
        {**base, "dh": "SHEET-NON-DATE", "rq": "2026-09-07"},
        {**base, "dh": "202601010001", "rq": "2026-09-08", "userid": "M2"},
        {**base, "dh": "202609090001", "rq": "2026-08-31", "userid": "M3"},
    )
    cache = make_result_cache(mode=mode, store=MemoryStore())
    if mode.reads:
        assert await cache.write(
            TenantId("SYNTHETIC-TENANT"),
            "ScjdQuery",
            source.progress,
            None,
            {},
            PREFETCH_WINDOW,
            source="precompute",
        )
    filters = NarrowedFilters(
        tenant_id=TenantId("SYNTHETIC-TENANT"),
        employee_ids=frozenset({EmployeeId("u1")}),
        dept_ids=frozenset({DeptId("dept-a")}),
    )
    result = await make_multirow_runner(ResultCacheExecutor(source, cache)).run(
        replace(_request("fr005_order_progress"), filters=filters, role=Role.MANAGER)
    )
    assert result.incomplete is False
    assert result.totals["sheet_count"] == Decimal("2")
    assert result.totals["issued_qty"] == Decimal("200")
    details = {row["sheet_code"]: row["created_date"] for row in result.detail_tables[0].rows}
    assert details == {"SHEET-NON-DATE": "2026-09-07", "202601010001": "2026-09-08"}
    assert result.api_call_count == (0 if mode is ResultCacheMode.READ else 1)
    if mode is ResultCacheMode.SHADOW:
        assert result.cache_source is not None


class MissingNativeDateSource(RangeProjectionSource):
    async def execute_full_step(
        self,
        filters: Any,
        request: Any,
        active_scope: Any = None,
        extra_params: Any = None,
    ) -> ResourceFetchResult:
        params = dict(extra_params or {})
        self.calls.append((request.operation_id, params))
        if params.get("groupbyFields"):
            rows = tuple(
                {key: value for key, value in row.items() if key != "rq"} for row in self.progress
            )
        else:
            rows = self.progress
        return ResourceFetchResult(rows=rows, total=len(rows), pages_fetched=1, complete=True)


@pytest.mark.asyncio
async def test_native_sheet_without_rq_fetches_default_once_and_counts_both_calls() -> None:
    source = MissingNativeDateSource()
    source.progress = ({**source.progress[0], "dh": "SHEET-NON-DATE", "rq": "2026-09-17"},)
    result = await make_multirow_runner(
        ResultCacheExecutor(source, make_result_cache(mode=ResultCacheMode.OFF))
    ).run(replace(_request("fr009_factory_order_overview"), role=Role.OWNER))
    assert result.incomplete is False
    assert result.api_call_count == 2
    assert len(source.calls) == 2
    assert source.calls[0][1]["groupbyFields"]
    assert "groupbyFields" not in source.calls[1][1]
    assert result.detail_tables[0].rows[0]["sheet_code"] == "SHEET-NON-DATE"
    assert result.detail_tables[0].rows[0]["created_date"] == "2026-09-17"


@pytest.mark.asyncio
async def test_empty_native_progress_response_does_not_trigger_date_fallback() -> None:
    source = MissingNativeDateSource()
    source.progress = ()
    result = await make_multirow_runner(
        ResultCacheExecutor(source, make_result_cache(mode=ResultCacheMode.OFF))
    ).run(replace(_request("fr009_factory_order_overview"), role=Role.OWNER))
    assert result.incomplete is False
    assert result.rows == ()
    assert result.api_call_count == 1
    assert len(source.calls) == 1


@pytest.mark.asyncio
async def test_native_and_default_missing_rq_fail_incomplete_without_sheet_date_fallback() -> None:
    source = MissingNativeDateSource()
    source.progress = ({key: value for key, value in source.progress[0].items() if key != "rq"},)
    store = MemoryStore()
    cache = make_result_cache(mode=ResultCacheMode.READWRITE, store=store)
    result = await make_multirow_runner(ResultCacheExecutor(source, cache)).run(
        replace(_request("fr009_factory_order_overview"), role=Role.OWNER)
    )
    assert result.incomplete is True
    assert result.incomplete_reason == "upstream_invalid"
    assert result.rows == ()
    assert result.api_call_count == 2
    assert len(source.calls) == 2
    assert source.calls[0][1]["groupbyFields"]
    assert "groupbyFields" not in source.calls[1][1]
    assert store.data == {}
