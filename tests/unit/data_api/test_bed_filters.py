import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.data_api.result_cache import (
    FetchThrough,
    ResultCacheExecutor,
    ResultCacheMode,
    local_view_rows,
)
from factory_agent.data_api.schemas import GongziClRow, ScjdV2Row, row_to_plain_dict
from factory_agent.domain import DeptId, EmployeeId, NarrowedFilters, Role, TenantId
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor
from factory_agent.ports.contracts import ResourceFetchResult
from tests.unit.data_api.test_payroll_department import (
    _bundle,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.data_api.test_result_cache import (
    PREFETCH_WINDOW,
    InnerFake,
    make_result_cache,
)

WINDOW = (datetime(2026, 9, 1, 0, tzinfo=UTC), datetime(2026, 10, 1, 0, tzinfo=UTC))
TENANT = TenantId("SYNTHETIC-APP")


def bed_filters(*beds: str) -> NarrowedFilters:
    return NarrowedFilters(TENANT, None, None, bed_codes=frozenset(beds))


def payroll_rows() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": f"synthetic-{index}",
            "jhdh": "PLAN-A" if index < 2 else "PLAN-B",
            "chuanghao": bed,
            "rq": "2026-08-01",
            "inputdate": "2026-09-15",
            "uid": "WORKER-A",
            "dept": "DEPT-A",
            "sl": str(index + 1),
            "je": str(index + 1),
        }
        for index, bed in enumerate(("1555", "1555", "11555", "15550", "01555"))
    )


@pytest.mark.parametrize("model", [GongziClRow, ScjdV2Row])
def test_bed_identity_is_a_string_and_absent_v2_columns_stay_absent(
    model: type[GongziClRow] | type[ScjdV2Row],
) -> None:
    assert row_to_plain_dict(model.model_validate({"chuanghao": "01555"})) == {"chuanghao": "01555"}
    assert "chuanghao" not in row_to_plain_dict(model.model_validate({}))
    with pytest.raises(ValidationError, match="bed identifiers must be strings"):
        model.model_validate({"chuanghao": 1555})


def test_bed_filter_uses_reporting_date_and_exact_string_equality() -> None:
    rows = payroll_rows()
    view = local_view_rows(
        rows,
        "GongziClQuery",
        bed_filters("1555"),
        {"Flag": "0"},
        ("2026-09-01", "2026-09-30"),
        Role.OWNER,
    )
    assert view == rows[:2]
    assert sum(int(row["sl"]) for row in view) == 3
    assert {row["rq"] for row in view} == {"2026-08-01"}
    assert local_view_rows(
        rows,
        "GongziClQuery",
        bed_filters("01555"),
        {},
        ("2026-09-01", "2026-09-30"),
        Role.OWNER,
    ) == (rows[4],)


def test_bed_filter_cannot_admit_an_outside_scope_contribution() -> None:
    rows = payroll_rows()
    rows = (*rows, {**rows[0], "uid": "OUTSIDE-IDENTITY", "dept": "OUTSIDE-DEPT", "sl": "999"})
    filters = replace(
        bed_filters("1555"),
        employee_ids=frozenset({EmployeeId("WORKER-A")}),
        dept_ids=frozenset({DeptId("DEPT-A")}),
    )
    view = local_view_rows(
        rows, "GongziClQuery", filters, {}, ("2026-09-01", "2026-09-30"), Role.EMPLOYEE
    )
    assert view == rows[:2]


def test_missing_bed_column_is_invalid_instead_of_a_complete_empty_result() -> None:
    with pytest.raises(InvalidRequestError, match="bed dimension"):
        local_view_rows(
            ({"inputdate": "2026-09-15", "sl": "2"},),
            "GongziClQuery",
            bed_filters("1555"),
            {},
            ("2026-09-01", "2026-09-30"),
            Role.OWNER,
        )


@pytest.mark.asyncio
async def test_hot_cache_and_live_bed_views_are_equal_and_subsets_never_fill_public_cache() -> None:
    rows = payroll_rows()
    source = ResourceFetchResult(rows=rows, total=5, pages_fetched=1, complete=True)
    cache = make_result_cache()
    assert await cache.write(
        TENANT, "GongziClQuery", rows, None, {}, PREFETCH_WINDOW, source="precompute"
    )
    cached_inner = InnerFake({"GongziClQuery": source})
    cached = ResultCacheExecutor(cached_inner, cache)
    live_inner = InnerFake({"GongziClQuery": source})
    live_cache = make_result_cache()
    live = ResultCacheExecutor(live_inner, live_cache)
    request = ExecutionRequest("GongziClQuery", WINDOW, role=Role.OWNER)
    filters = bed_filters("1555")

    hot = await cached.execute_full_step(filters, request)
    cold = await live.execute_full_step(filters, request)

    assert hot.rows == cold.rows == rows[:2]
    assert hot.total == cold.total == 2
    assert cached_inner.calls == []
    assert len(live_inner.calls) == 1
    assert await live_cache.read(TENANT, "GongziClQuery", {}, ("2026-09-01", "2026-09-30")) is None


@pytest.mark.parametrize("operation", ["GongziClQuery", "ScjdQuery"])
@pytest.mark.parametrize("beds", [("01555",), ("1555", "01555")])
@pytest.mark.asyncio
async def test_only_one_bed_is_pushed_as_a_fuzzy_candidate(
    operation: str, beds: tuple[str, ...]
) -> None:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"code": 1, "message": "成功", "result": {"list": [], "total": 0}, "timestamp": 1},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle("99"), load_catalog(), client=client
        )
        await adapter.fetch_resource(operation, bed_filters(*beds), WINDOW, 50)
    assert len(bodies) == 1
    if len(beds) == 1:
        assert bodies[0]["chuanghao"] == "01555"
    else:
        assert "chuanghao" not in bodies[0]


@pytest.mark.asyncio
async def test_live_fuzzy_candidates_are_filtered_before_the_executor_returns_rows() -> None:
    rows = payroll_rows()
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = cast("dict[str, Any]", json.loads(request.content))
        requested.append(body["chuanghao"])
        return httpx.Response(
            200,
            json={
                "code": 1,
                "message": "成功",
                "result": {"list": list(rows), "total": len(rows)},
                "timestamp": 1,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle("99"), load_catalog(), client=client
        )
        executor = ResultCacheExecutor(
            cast(FetchThrough, ScopedExecutor(adapter, load_catalog())),
            make_result_cache(mode=ResultCacheMode.OFF),
        )
        fetched = await executor.execute_full_step(
            bed_filters("1555"), ExecutionRequest("GongziClQuery", WINDOW, role=Role.OWNER)
        )
    assert requested == ["1555"]
    assert fetched.complete is True
    assert fetched.rows == rows[:2]
