from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping, cast
from unittest.mock import patch

import pytest

from factory_agent.application.cache import AuthAwareCache
from factory_agent.application.capability_map import DRILLDOWN_RECIPE_BY_FR, RECIPE_BY_FR
from factory_agent.application.permission_matrix import CAPABILITY_ROLES, Capability
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.result_cache import (
    CacheableRequest,
    ResultCacheExecutor,
    ResultCacheMode,
    V2ResultCache,
)
from factory_agent.data_api.schemas import GongziClRow, ScjdV2Row
from factory_agent.domain import (
    CapabilityId,
    DeptId,
    EmployeeId,
    NarrowedFilters,
    Role,
    TenantId,
    TimeRange,
)
from factory_agent.execution.kernel import KernelCapabilityRunner
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.execution.sandbox_runtime import duckdb
from factory_agent.ports.card import comparable_card
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest, CapabilityRunResult

TENANT = TenantId("fixture-factory")
TODAY = datetime(2026, 10, 15, 12, tzinfo=UTC)
WINDOW = TimeRange(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
PAYROLL_PARAMS: dict[str, object] = {"Flag": "0", "Type": "0,1,2"}
M7 = ("dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname")


def wage(
    record: str,
    employee: str,
    dept: str,
    qty: str,
    price: str,
    date: str,
    *,
    source: str = "0",
) -> dict[str, object]:
    return dict(
        GongziClRow.model_validate(
            {
                "id": record,
                "uid": employee,
                "uname": f"Sample {employee}",
                "dept": dept,
                "deptname": dept,
                "jhdh": "P1" if dept == "D1" else "P2",
                "chuanghao": "1",
                "huohao": "S1" if dept == "D1" else "S2",
                "bbreed": "S1" if dept == "D1" else "S2",
                "worktype": "W1",
                "wtname": "Work 1",
                "inputdate": date,
                "sl": qty,
                "price": price,
                "je": str(Decimal(qty) * Decimal(price)),
                "type": source,
                "bs": "1",
            }
        ).model_dump()
    )


WAGES = (
    wage("R1", "E1", "D1", "4", "2", "2026-07-01", source="0"),
    wage("R2", "E2", "D1", "6", "3", "2026-07-02", source="1"),
    wage("R3", "E3", "D2", "8", "3", "2026-07-03", source="2"),
    wage("R4", "E1", "D1", "2", "2", "2026-07-01", source="0"),
)


def progress(**values: object) -> dict[str, object]:
    return dict(ScjdV2Row.model_validate(values).model_dump())


SHEET1 = {
    "dh": "20260701001",
    "rq": "2026-07-01",
    "jhdh": "P1",
    "chuanghao": "1",
    "huohao": "S1",
    "bbreed": "S1",
    "baohao": "1",
    "userid": "M1",
    "color": "C1",
    "chima": "S",
}
SHEET2 = {
    "dh": "20260703001",
    "rq": "2026-07-03",
    "jhdh": "P2",
    "chuanghao": "2",
    "huohao": "S2",
    "bbreed": "S2",
    "baohao": "2",
    "userid": "M2",
    "color": "C2",
    "chima": "M",
}
PROGRESS = (
    progress(
        **SHEET1,
        uid="E1",
        uname="Sample E1",
        dept="D1",
        deptname="D1",
        worktype="W1",
        wtname="Work 1",
        wsort="1",
        bs="1",
        fhsl="10",
        sssl="6",
        zpsl="6",
        wts="1",
        wgs="1",
    ),
    progress(
        **SHEET1,
        uid="E2",
        uname="Sample E2",
        dept="D1",
        deptname="D1",
        worktype="W2",
        wtname="Work 2",
        wsort="2",
        bs="0",
        fhsl="10",
        sssl="0",
        zpsl="0",
        wts="1",
        wgs="0",
    ),
    progress(
        **SHEET1,
        uid="",
        uname="",
        dept="",
        deptname="",
        worktype="W3",
        wtname="Work 3",
        wsort="3",
        bs="0",
        fhsl="5",
        sssl="0",
        zpsl="0",
        wts="1",
        wgs="0",
    ),
    progress(
        **SHEET2,
        uid="E3",
        uname="Sample E3",
        dept="D2",
        deptname="D2",
        worktype="W1",
        wtname="Work 1",
        wsort="1",
        bs="1",
        fhsl="20",
        sssl="20",
        zpsl="20",
        wts="1",
        wgs="1",
    ),
)
ROSTER: tuple[dict[str, object], ...] = (
    {"uid": "E1", "dept": "D1"},
    {"uid": "E2", "dept": "D1"},
    {"uid": "E3", "dept": "D2"},
    {"uid": "E4", "dept": "D2"},
)


@dataclass
class MemoryStore:
    values: dict[str, bytes] = field(default_factory=lambda: {})

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        for key in tuple(self.values):
            if key.startswith(prefix):
                self.values.pop(key)


class RosterSource:
    def __init__(self, rows: tuple[dict[str, object], ...] = ROSTER) -> None:
        self._rows = rows

    async def fetch_roster(self, filters: NarrowedFilters) -> ResourceFetchResult:
        assert filters.tenant_id == TENANT
        return ResourceFetchResult(
            rows=self._rows,
            total=len(self._rows),
            pages_fetched=0,
            complete=True,
            cache_source="identity_org",
        )


def fetch_result(rows: tuple[dict[str, object], ...]) -> ResourceFetchResult:
    return ResourceFetchResult(rows=rows, total=len(rows), pages_fetched=1, complete=True)


def visible(row: dict[str, object], filters: NarrowedFilters, role: Role | None) -> bool:
    if filters.employee_ids is not None and row.get("uid") not in filters.employee_ids:
        return False
    depts = filters.requested_dept_ids if role is Role.OWNER else filters.dept_ids
    if depts is not None and row.get("dept") not in depts:
        return False
    if filters.order_codes and row.get("jhdh") not in filters.order_codes:
        return False
    if filters.style_codes and row.get("bbreed") not in filters.style_codes:
        return False
    return not filters.material_ids or row.get("userid") in filters.material_ids


class LiveExecutor:
    """Hand-authored customer aggregates are independent of the cache projection."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def validate_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
    ) -> None:
        assert filters.tenant_id == TENANT

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        self.calls.append(request.operation_id)
        if request.operation_id == "GongziClQuery":
            assert "groupbyFields" not in (extra_params or {})
            return fetch_result(tuple(row for row in WAGES if visible(row, filters, request.role)))
        if request.operation_id != "ScjdQuery":
            raise AssertionError("roster steps must use the shared directory provider")
        group = frozenset(cast("tuple[str, ...]", (extra_params or {}).get("groupbyFields", ())))
        owner = request.role is Role.OWNER and filters.requested_dept_ids is None
        if "baohao" in group:
            rows = (
                progress(
                    **SHEET1,
                    fhsl="25" if owner else "20",
                    sssl="6",
                    bs="1",
                    wts="3" if owner else "2",
                    wgs="1",
                    zpsl="6",
                    wcl="24" if owner else "30",
                ),
                progress(
                    **SHEET2, fhsl="20", sssl="20", bs="1", wts="1", wgs="1", zpsl="20", wcl="100"
                ),
            )
            rows = tuple(
                row
                for row in rows
                if (owner or row["jhdh"] == "P1")
                and (not filters.order_codes or row["dh"] in filters.order_codes)
            )
        elif "wsort" in group:
            rows = tuple(row for row in PROGRESS if visible(row, filters, request.role))
        else:
            rows = (
                progress(
                    **SHEET1,
                    dept="D1",
                    deptname="D1",
                    fhsl="20",
                    sssl="6",
                    bs="1",
                    wts="2",
                    wgs="1",
                    zpsl="6",
                    wcl="30",
                ),
                progress(
                    **SHEET1,
                    dept="",
                    deptname="",
                    fhsl="5",
                    sssl="0",
                    bs="0",
                    wts="1",
                    wgs="0",
                    zpsl="0",
                    wcl="0",
                ),
                progress(
                    **SHEET2,
                    dept="D2",
                    deptname="D2",
                    fhsl="20",
                    sssl="20",
                    bs="1",
                    wts="1",
                    wgs="1",
                    zpsl="20",
                    wcl="100",
                ),
            )
            rows = tuple(row for row in rows if visible(row, filters, request.role))
        return fetch_result(rows)


class NoMesExecutor(LiveExecutor):
    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        self.calls.append(request.operation_id)
        raise AssertionError(f"warm query unexpectedly called {request.operation_id}")


def runner(
    executor: Any,
    *,
    roster: tuple[dict[str, object], ...] = ROSTER,
) -> KernelCapabilityRunner:
    return KernelCapabilityRunner(
        executor,
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
        clock=lambda: TODAY,
        employee_roster=RosterSource(roster),
        base_data_operations=frozenset({"EmployeeQuery"}),
    )


def request_for(fr: str, recipe: str, role: Role) -> CapabilityRunRequest:
    employees = frozenset({EmployeeId("E1")}) if fr in {"FR-001", "FR-002", "FR-003"} else None
    if fr == "FR-012":
        employees = frozenset({EmployeeId("E2")})
    orders = frozenset({"20260701001"}) if recipe == "fr005_order_package_detail" else None
    materials = frozenset({"M1"}) if recipe == "fr005_order_worktype_detail" else None
    return CapabilityRunRequest(
        capability_id=CapabilityId(recipe),
        role=role,
        time_range=WINDOW,
        caller_employee_id=EmployeeId("E1"),
        caller_dept_ids=frozenset({DeptId("D1")}),
        filters=NarrowedFilters(
            tenant_id=TENANT,
            employee_ids=employees,
            dept_ids=frozenset({DeptId("D1")}),
            order_codes=orders,
            material_ids=materials,
        ),
    )


CASES = [
    (fr, recipe, role)
    for fr, main in RECIPE_BY_FR.items()
    for recipe in (main, *DRILLDOWN_RECIPE_BY_FR.get(fr, ()))
    for role in sorted(CAPABILITY_ROLES[Capability(fr)], key=lambda value: value.value)
]


async def seed_snapshots(
    mode: ResultCacheMode,
    wages: tuple[dict[str, object], ...] = WAGES,
    *,
    progress_rows: tuple[dict[str, object], ...] = PROGRESS,
) -> tuple[V2ResultCache, MemoryStore]:
    store = MemoryStore()
    cache = V2ResultCache(
        AuthAwareCache(
            store, contract_version="fixture", metric_version="fixture", data_version="fixture"
        ),
        mode=mode,
        clock=lambda: TODAY,
    )
    window = cache.prefetch_window(TODAY.date().isoformat())
    assert await cache.write(
        TENANT, "GongziClQuery", wages, None, PAYROLL_PARAMS, window, source="precompute"
    )
    assert await cache.write(
        TENANT, "ScjdQuery", progress_rows, None, {}, window, source="precompute"
    )
    assert len(store.values) == 2
    return cache, store


def plan_group_contribution(
    sheet: str,
    plan: str | None,
    style: str,
    bed: str,
    dept: str,
    issued: str,
    received: str,
    packages: str,
    worktypes: str,
    worktype: str,
    *,
    rq: str,
) -> dict[str, object]:
    return progress(
        dh=sheet,
        rq=rq,
        jhdh=plan,
        chuanghao=bed,
        huohao=style,
        bbreed=style,
        dept=dept,
        deptname=dept,
        uid="E1" if dept else "",
        uname="Sample E1" if dept else "",
        baohao=worktype,
        userid=f"M-{worktype}",
        color="C1",
        chima="S",
        worktype=worktype,
        wtname=worktype,
        wsort="1",
        bs=packages,
        fhsl=issued,
        sssl=received,
        zpsl=received,
        wts=worktypes,
        wgs="0",
        wcl="0",
    )


PLAN_GROUP_DETAIL = (
    plan_group_contribution(
        "20260701001", "P1", "S1", "1", "D1", "60", "6", "1", "1", "W1", rq="2026-07-01"
    ),
    plan_group_contribution(
        "20260701001", "P1", "S1", "1", "D1", "40", "4", "1", "1", "W2", rq="2026-07-01"
    ),
    plan_group_contribution(
        "20260701001", "P1", "S1", "1", "D2", "20", "20", "1", "1", "W3", rq="2026-07-01"
    ),
    plan_group_contribution(
        "20260702001", "P1", "S2", "2", "D2", "80", "70", "3", "3", "W4", rq="2026-07-02"
    ),
    plan_group_contribution(
        "20260703001", None, "S3", "3", "D1", "10", "10", "1", "1", "W5", rq="2026-07-03"
    ),
    plan_group_contribution(
        "20260704001", "", "S4", "4", "D2", "90", "0", "2", "2", "W6", rq="2026-07-04"
    ),
    plan_group_contribution(
        "20260705001", "   ", "S5", "5", "", "50", "40", "4", "4", "W7", rq="2026-07-05"
    ),
    plan_group_contribution(
        "20260706001", "未关联计划号", "S6", "6", "D1", "25", "25", "1", "1", "W8", rq="2026-07-06"
    ),
    plan_group_contribution(
        "20260707001", "P2", "S7", "7", "D2", "30", "0", "1", "1", "W9", rq="2026-07-07"
    ),
)


class PlanGroupLiveExecutor(LiveExecutor):
    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        assert request.operation_id == "ScjdQuery"
        assert request.role is Role.OWNER
        assert filters.order_codes is filters.style_codes is None
        assert frozenset((extra_params or {}).get("groupbyFields", ())) == frozenset(M7)
        self.calls.append(request.operation_id)
        first = plan_group_contribution(
            "20260701001", "P1", "S1", "1", "D1", "100", "10", "2", "2", "W1", rq="2026-07-01"
        )
        return fetch_result((first, *PLAN_GROUP_DETAIL[2:]))


@pytest.mark.parametrize("fr", ("FR-005", "FR-009"))
@pytest.mark.parametrize("mode", (ResultCacheMode.READ, ResultCacheMode.SHADOW))
@pytest.mark.asyncio
async def test_plan_group_progress_keeps_one_row_per_plan_from_shared_detail(
    fr: str, mode: ResultCacheMode
) -> None:
    cache, store = await seed_snapshots(mode, progress_rows=PLAN_GROUP_DETAIL)
    source = PlanGroupLiveExecutor() if mode is ResultCacheMode.SHADOW else NoMesExecutor()
    request = request_for(fr, RECIPE_BY_FR[fr], Role.OWNER)
    result = await runner(ResultCacheExecutor(source, cache)).run(request)
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.rows]
    assert len(rows) == 4
    assert result.incomplete is False
    assert result.cache_source is not None
    assert result.cache_source["sources"] == ["precompute"]
    assert len(store.values) == 2
    assert result.api_call_count == (1 if mode is ResultCacheMode.SHADOW else 0)
    assert source.calls == (["ScjdQuery"] if mode is ResultCacheMode.SHADOW else [])
    assert all(row["order_count"] == Decimal("1") for row in rows)

    joined = next(row for row in rows if row["order_code"] == "P1")
    assert joined["sheet_count"] == Decimal("2")
    assert joined["package_count"] == Decimal("6")
    assert joined["issued_qty"] == Decimal("200")
    assert joined["worktype_count"] == Decimal("6")
    assert Decimal(str(joined["progress_ratio"])) == Decimal("50")
    assert joined["finish_state"] == "未完工"
    assert all(
        name not in joined for name in ("sheet_code", "style_code", "bed_code", "created_date")
    )
    details = result.detail_tables[0].rows
    joined_details = [row for row in details if row["plan_code"] == "P1"]
    assert len(joined_details) == 2

    unlabeled = [row for row in rows if row["order_code"] == "未关联计划号"]
    assert len(unlabeled) == 2
    empty_plan = next(row for row in unlabeled if row["sheet_count"] == Decimal("3"))
    assert empty_plan["package_count"] == Decimal("7")
    assert empty_plan["issued_qty"] == Decimal("150")
    assert empty_plan["worktype_count"] == Decimal("7")
    assert Decimal(str(empty_plan["progress_ratio"])) == Decimal("33.33")
    assert empty_plan["finish_state"] == "未完工"
    assert all(
        name not in empty_plan for name in ("sheet_code", "style_code", "bed_code", "created_date")
    )
    literal_plan = next(row for row in unlabeled if row["sheet_count"] == Decimal("1"))
    literal_detail = next(row for row in details if row["plan_code"] == "未关联计划号")
    assert literal_detail["sheet_code"] == "20260706001"
    assert literal_detail["style_name"] == "S6"
    assert literal_detail["bed_code"] == "6"
    assert literal_detail["created_date"] == "2026-07-06"
    assert literal_plan["issued_qty"] == Decimal("25")
    assert literal_plan["finish_state"] == "全部完工"

    assert sum(cast(Decimal, row["sheet_count"]) for row in rows) == Decimal("7")
    assert sum(cast(Decimal, row["package_count"]) for row in rows) == Decimal("15")
    assert sum(cast(Decimal, row["issued_qty"]) for row in rows) == Decimal("405")
    if mode is ResultCacheMode.SHADOW:
        live = await runner(PlanGroupLiveExecutor()).run(request)
        assert result.rows == live.rows
        assert result.totals == live.totals
        assert comparable_card(result.card) == comparable_card(live.card)


@pytest.mark.parametrize("fr", ("FR-005", "FR-009"))
@pytest.mark.asyncio
async def test_style_selection_narrows_cached_progress_without_splitting_the_empty_plan(
    fr: str,
) -> None:
    cache, store = await seed_snapshots(ResultCacheMode.READ, progress_rows=PLAN_GROUP_DETAIL)
    request = request_for(fr, RECIPE_BY_FR[fr], Role.OWNER)
    request = replace(
        request,
        filters=replace(request.filters, style_codes=frozenset({"S3", "S4", "S5"})),
    )
    no_mes = NoMesExecutor()
    result = await runner(ResultCacheExecutor(no_mes, cache)).run(request)
    assert len(result.rows) == 1
    row = dict(zip(result.column_names, result.rows[0], strict=True))
    assert row["order_code"] == "未关联计划号"
    assert row["order_count"] == Decimal("1")
    assert row["sheet_count"] == Decimal("3")
    assert "style_code" not in row
    assert {entry["style_name"] for entry in result.detail_tables[0].rows} == {"S3", "S4", "S5"}
    assert row["package_count"] == Decimal("7")
    assert row["issued_qty"] == Decimal("150")
    assert Decimal(str(row["progress_ratio"])) == Decimal("33.33")
    assert result.api_call_count == 0
    assert result.incomplete is False
    assert result.cache_source is not None
    assert no_mes.calls == []
    assert len(store.values) == 2


@pytest.mark.parametrize(("fr", "recipe", "role"), CASES)
@pytest.mark.asyncio
async def test_every_allowed_fr_and_drill_matches_live_without_mes_calls(
    fr: str,
    recipe: str,
    role: Role,
) -> None:
    cache, store = await seed_snapshots(ResultCacheMode.READ)
    no_mes = NoMesExecutor()
    request = request_for(fr, recipe, role)
    cached = await runner(ResultCacheExecutor(no_mes, cache)).run(request)
    live = await runner(LiveExecutor()).run(request)
    assert no_mes.calls == []
    assert cached.api_call_count == 0
    assert cached.cache_source is not None
    assert cached.cache_source["sources"] == ["precompute"]
    assert cached.cache_source["generated_at"] == [TODAY.isoformat()]
    assert set(cached.cache_source) == {"sources", "generated_at"}
    assert cached.rows == live.rows
    assert cached.totals == live.totals
    assert comparable_card(cached.card) == comparable_card(live.card)
    assert cached.incomplete is live.incomplete is False
    assert len(store.values) == 2
    if fr == "FR-004":
        own = dict(zip(cached.column_names, cached.rows[0], strict=True))
        assert str(own["group_rank"]) == "2"
        assert str(own["group_size"]) == "2"
        assert str(own["rank_position"]) == ("3" if role is Role.OWNER else "2")
    if fr == "FR-011":
        department = next(
            dict(zip(cached.column_names, row, strict=True))
            for row in cached.rows
            if row[0] == "D2"
        )
        assert department["headcount"] == Decimal("2")
        assert department["avg_wage"] == Decimal("12")


class ShadowExecutor(LiveExecutor):
    def __init__(self, *, shift_date: bool = False) -> None:
        super().__init__()
        self.shift_date = shift_date
        self.discarded: list[ResourceFetchResult] = []
        self.released: list[ResourceFetchResult] = []

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        live = await super().execute_full_step(filters, request, active_scope, extra_params)
        rows = tuple(reversed(live.rows))
        if self.shift_date and request.operation_id == "GongziClQuery":
            rows = tuple(
                {**row, "inputdate": "2026-07-04"} if row["id"] == "R2" else row for row in rows
            )
        return replace(live, rows=rows, cache_source="precompute", shadow_result=live)

    async def discard_cached_result(self, fetched: ResourceFetchResult) -> None:
        self.discarded.append(fetched)

    def release_cached_result(self, fetched: ResourceFetchResult) -> None:
        self.released.append(fetched)


@pytest.mark.parametrize(("fr", "recipe", "role"), CASES)
@pytest.mark.asyncio
async def test_actual_shadow_executor_preserves_consistent_cache_for_reviewed_outputs(
    fr: str,
    recipe: str,
    role: Role,
) -> None:
    cache, store = await seed_snapshots(ResultCacheMode.SHADOW)
    live = LiveExecutor()
    request = request_for(fr, recipe, role)
    result = await runner(ResultCacheExecutor(live, cache)).run(request)
    expected = await runner(LiveExecutor()).run(request)
    assert result.rows == expected.rows
    assert comparable_card(result.card) == comparable_card(expected.card)
    assert result.cache_source is not None
    assert result.api_call_count == len(live.calls)
    assert len(store.values) == 2


class OwnerSnapshots:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self,
        tenant_id: Any,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult:
        assert tenant_id == TENANT
        assert window == ("2026-07-01", "2026-07-31")
        assert set(extra_params) <= {"Flag", "Type"}
        self.calls.append((operation_id, dict(extra_params)))
        return fetch_result(WAGES if operation_id == "GongziClQuery" else PROGRESS)


@pytest.mark.parametrize(("fr", "recipe", "role"), CASES)
@pytest.mark.asyncio
async def test_every_allowed_cold_snapshot_counts_mes_then_same_query_cache_is_zero(
    fr: str,
    recipe: str,
    role: Role,
) -> None:
    store = MemoryStore()
    cache = V2ResultCache(
        AuthAwareCache(
            store, contract_version="fixture", metric_version="fixture", data_version="fixture"
        ),
        mode=ResultCacheMode.READWRITE,
        clock=lambda: TODAY,
    )
    snapshots = OwnerSnapshots()
    no_mes = NoMesExecutor()
    cached_runner = runner(ResultCacheExecutor(no_mes, cache, snapshot_provider=snapshots))
    request = request_for(fr, recipe, role)
    first = await cached_runner.run(request)
    expected = await runner(LiveExecutor()).run(request)
    expected_calls = 2 if fr in {"FR-006", "FR-013"} else 1
    assert first.api_call_count == len(snapshots.calls) == expected_calls
    assert first.rows == expected.rows
    assert comparable_card(first.card) == comparable_card(expected.card)
    assert first.cache_source is not None
    assert first.cache_source["sources"] == ["query"]
    second = await cached_runner.run(request)
    assert second.api_call_count == 0
    assert len(snapshots.calls) == expected_calls
    assert no_mes.calls == []
    assert second.rows == expected.rows
    assert comparable_card(second.card) == comparable_card(expected.card)
    assert second.cache_source == first.cache_source


@pytest.mark.parametrize("fr", ("FR-002", "FR-013"))
@pytest.mark.asyncio
async def test_cold_shadow_counts_owner_snapshot_and_caller_live_fetch(fr: str) -> None:
    store = MemoryStore()
    cache = V2ResultCache(
        AuthAwareCache(
            store, contract_version="fixture", metric_version="fixture", data_version="fixture"
        ),
        mode=ResultCacheMode.SHADOW,
        clock=lambda: TODAY,
    )
    snapshots = OwnerSnapshots()
    live = LiveExecutor()
    cached_runner = runner(ResultCacheExecutor(live, cache, snapshot_provider=snapshots))
    request = request_for(fr, RECIPE_BY_FR[fr], Role.OWNER)
    first = await cached_runner.run(request)
    expected = await runner(LiveExecutor()).run(request)
    expected_calls = 2 if fr == "FR-013" else 1
    assert first.api_call_count == len(snapshots.calls) + len(live.calls) == expected_calls * 2
    assert first.rows == expected.rows
    assert comparable_card(first.card) == comparable_card(expected.card)
    live.calls.clear()
    second = await cached_runner.run(request)
    assert second.api_call_count == len(live.calls) == expected_calls
    assert len(snapshots.calls) == expected_calls


@pytest.mark.asyncio
async def test_actual_shadow_evicts_hit_snapshots_when_daily_chart_differs() -> None:
    stale_wages = tuple(
        {**row, "inputdate": "2026-07-04"} if row["id"] == "R2" else row for row in WAGES
    )
    cache, store = await seed_snapshots(ResultCacheMode.SHADOW, stale_wages)
    request = request_for("FR-013", RECIPE_BY_FR["FR-013"], Role.OWNER)
    actual = await runner(ResultCacheExecutor(LiveExecutor(), cache)).run(request)
    expected = await runner(LiveExecutor()).run(request)
    assert actual.rows == expected.rows
    assert comparable_card(actual.card) == comparable_card(expected.card)
    assert actual.cache_source is None
    assert store.values == {}


@pytest.mark.parametrize(
    ("fr", "condition"),
    (
        ("FR-005", "order"),
        ("FR-006", "order"),
        ("FR-005", "style"),
        ("FR-007", "style"),
        ("FR-010", "style"),
        ("FR-008", "department"),
        ("FR-011", "department"),
    ),
)
@pytest.mark.asyncio
async def test_narrow_business_filters_reuse_the_same_two_snapshots(
    fr: str, condition: str
) -> None:
    cache, store = await seed_snapshots(ResultCacheMode.READ)
    request = request_for(fr, RECIPE_BY_FR[fr], Role.OWNER)
    filters = request.filters
    if condition == "order":
        filters = replace(filters, order_codes=frozenset({"P1"}))
    elif condition == "style":
        filters = replace(filters, style_codes=frozenset({"S1"}))
    else:
        filters = replace(
            filters,
            dept_ids=frozenset({DeptId("D2")}),
            requested_dept_ids=frozenset({DeptId("D2")}),
        )
    request = replace(request, filters=filters)
    no_mes = NoMesExecutor()
    result = await runner(ResultCacheExecutor(no_mes, cache)).run(request)
    expected = await runner(LiveExecutor()).run(request)
    assert result.rows == expected.rows
    assert comparable_card(result.card) == comparable_card(expected.card)
    assert result.rows
    assert no_mes.calls == []
    assert len(store.values) == 2


@pytest.mark.asyncio
async def test_final_shadow_uses_one_isolated_duckdb_connection() -> None:
    request = request_for("FR-013", RECIPE_BY_FR["FR-013"], Role.OWNER)
    with patch.object(duckdb, "connect", wraps=duckdb.connect) as connections:
        await runner(ShadowExecutor()).run(request)
    assert connections.call_count == 1


@pytest.mark.parametrize("fr", ("FR-001", "FR-002", "FR-003", "FR-008", "FR-013"))
@pytest.mark.asyncio
async def test_shadow_accepts_different_input_order_when_final_outputs_match(fr: str) -> None:
    executor = ShadowExecutor()
    request = request_for(fr, RECIPE_BY_FR[fr], Role.OWNER)
    result = await runner(executor).run(request)
    expected = await runner(LiveExecutor()).run(request)
    assert result.rows == expected.rows
    assert comparable_card(result.card) == comparable_card(expected.card)
    assert executor.discarded == []
    assert executor.released


@pytest.mark.asyncio
async def test_shadow_checks_aux_daily_chart_and_returns_live_on_mismatch() -> None:
    executor = ShadowExecutor(shift_date=True)
    request = request_for("FR-013", RECIPE_BY_FR["FR-013"], Role.OWNER)
    result = await runner(executor).run(request)
    expected = await runner(LiveExecutor()).run(request)
    assert result.rows == expected.rows
    assert result.totals == expected.totals
    assert comparable_card(result.card) == comparable_card(expected.card)
    assert result.cache_source is None
    assert len(executor.discarded) == 2
    assert executor.released == []


@pytest.mark.asyncio
async def test_roster_provider_bypasses_mes_and_keeps_inactive_employee_in_headcount() -> None:
    executor = LiveExecutor()
    result: CapabilityRunResult = await runner(executor).run(
        request_for("FR-011", RECIPE_BY_FR["FR-011"], Role.OWNER)
    )
    assert executor.calls == ["GongziClQuery"]
    row = next(
        dict(zip(result.column_names, row, strict=True)) for row in result.rows if row[0] == "D2"
    )
    assert row["headcount"] == Decimal("2")
    assert row["avg_wage"] == Decimal("12")


@pytest.mark.asyncio
async def test_rank_uses_current_roster_department_with_multiple_bound_departments() -> None:
    cache, _ = await seed_snapshots(ResultCacheMode.READ)
    request = request_for("FR-004", RECIPE_BY_FR["FR-004"], Role.MANAGER)
    depts = frozenset({DeptId("D1"), DeptId("D2")})
    request = replace(
        request, filters=replace(request.filters, dept_ids=depts), caller_dept_ids=depts
    )
    no_mes = NoMesExecutor()
    result = await runner(ResultCacheExecutor(no_mes, cache)).run(request)
    assert len(result.rows) == 1
    own = dict(zip(result.column_names, result.rows[0], strict=True))
    assert own["rank_position"] == Decimal("3")
    assert own["group_rank"] == Decimal("2")
    assert own["group_size"] == Decimal("2")
    assert no_mes.calls == []


@pytest.mark.parametrize("state", ("missing", "out_of_scope", "ambiguous", "empty_department"))
@pytest.mark.asyncio
async def test_rank_reports_unavailable_when_current_group_cannot_be_validated(state: str) -> None:
    cache, _ = await seed_snapshots(ResultCacheMode.READ)
    request = request_for("FR-004", RECIPE_BY_FR["FR-004"], Role.MANAGER)
    depts = frozenset({DeptId("D1"), DeptId("D2")})
    request = replace(
        request, filters=replace(request.filters, dept_ids=depts), caller_dept_ids=depts
    )
    others = tuple(row for row in ROSTER if row["uid"] != "E1")
    roster: tuple[dict[str, object], ...]
    if state == "missing":
        roster = others
    elif state == "out_of_scope":
        roster = ({"uid": "E1", "dept": "D3"}, *others)
    elif state == "ambiguous":
        roster = ({"uid": "E1", "dept": "D1"}, {"uid": "E1", "dept": "D2"}, *others)
    else:
        roster = ({"uid": "E1", "dept": ""}, *others)
    no_mes = NoMesExecutor()
    result = await runner(ResultCacheExecutor(no_mes, cache), roster=roster).run(request)
    assert len(result.rows) == 1
    own = dict(zip(result.column_names, result.rows[0], strict=True))
    assert own["group_rank"] == "unavailable"
    assert own["group_size"] == "unavailable"
    assert own["rank_position"] == Decimal("3")
    assert no_mes.calls == []


@pytest.mark.parametrize("rename", (False, True))
@pytest.mark.asyncio
async def test_rank_combines_transferred_employee_income_once_across_department_and_name_changes(
    rename: bool,
) -> None:
    income = (*WAGES, wage("R5", "E1", "D2", "1", "3", "2026-07-04"))
    if rename:
        income = tuple(
            {**row, "uname": "Previous E1"} if row["id"] == "R4" else row for row in income
        )
    cache, _ = await seed_snapshots(ResultCacheMode.READ, income)
    request = request_for("FR-004", RECIPE_BY_FR["FR-004"], Role.MANAGER)
    depts = frozenset({DeptId("D1"), DeptId("D2")})
    request = replace(
        request, filters=replace(request.filters, dept_ids=depts), caller_dept_ids=depts
    )
    no_mes = NoMesExecutor()
    result = await runner(ResultCacheExecutor(no_mes, cache)).run(request)
    assert len(result.rows) == 1
    own = dict(zip(result.column_names, result.rows[0], strict=True))
    assert own["amount"] == Decimal("15")
    assert own["rank_position"] == Decimal("3")
    assert own["group_rank"] == Decimal("2")
    assert own["group_size"] == Decimal("2")
    assert no_mes.calls == []


@pytest.mark.asyncio
async def test_rank_current_department_membership_excludes_inactive_roster_members() -> None:
    income = (*WAGES, wage("R5", "E1", "D2", "3", "3", "2026-07-04"))
    roster = tuple({**row, "dept": "D2"} if row["uid"] == "E1" else row for row in ROSTER)
    cache, _ = await seed_snapshots(ResultCacheMode.READ, income)
    request = request_for("FR-004", RECIPE_BY_FR["FR-004"], Role.MANAGER)
    depts = frozenset({DeptId("D1"), DeptId("D2")})
    request = replace(
        request, filters=replace(request.filters, dept_ids=depts), caller_dept_ids=depts
    )
    result = await runner(ResultCacheExecutor(NoMesExecutor(), cache), roster=roster).run(request)
    assert len(result.rows) == 1
    own = dict(zip(result.column_names, result.rows[0], strict=True))
    assert own["amount"] == Decimal("21")
    assert own["rank_position"] == Decimal("2")
    assert own["group_rank"] == Decimal("2")
    assert own["group_size"] == Decimal("2")


@pytest.mark.parametrize("ambiguous", (False, True))
@pytest.mark.asyncio
async def test_rank_group_stays_unavailable_when_a_paid_members_current_department_is_unknown(
    ambiguous: bool,
) -> None:
    roster: tuple[dict[str, object], ...] = tuple(row for row in ROSTER if row["uid"] != "E2")
    if ambiguous:
        roster = ({"uid": "E2", "dept": "D1"}, {"uid": "E2", "dept": "D2"}, *roster)
    cache, _ = await seed_snapshots(ResultCacheMode.READ)
    request = request_for("FR-004", RECIPE_BY_FR["FR-004"], Role.MANAGER)
    result = await runner(ResultCacheExecutor(NoMesExecutor(), cache), roster=roster).run(request)
    own = dict(zip(result.column_names, result.rows[0], strict=True))
    assert own["amount"] == Decimal("12")
    assert own["rank_position"] == Decimal("2")
    assert own["group_rank"] == "unavailable"
    assert own["group_size"] == "unavailable"
