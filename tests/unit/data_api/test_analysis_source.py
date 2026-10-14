from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from factory_agent.data_api.analysis_source import CachedFactSource
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    NarrowedFilters,
    Role,
    ScopeVersion,
    TenantId,
    TimeRange,
)
from factory_agent.domain.errors import ForbiddenError
from factory_agent.ports.analysis_data import FactQuery
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.resource_execution import ExecutionRequest
from tests.support.session import FrozenClock

NOW = datetime(2026, 10, 13, tzinfo=UTC)
WINDOW = TimeRange(
    datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
    datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
)


class RecordingExecutor:
    def __init__(self, result: ResourceFetchResult) -> None:
        self.result = result
        self.calls: list[
            tuple[NarrowedFilters, ExecutionRequest, DataScope, Mapping[str, Any] | None]
        ] = []
        self.discarded: list[ResourceFetchResult] = []
        self.released: list[ResourceFetchResult] = []

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: ExecutionRequest,
        active_scope: DataScope | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ResourceFetchResult:
        assert active_scope is not None
        self.calls.append((filters, request, active_scope, extra_params))
        return self.result

    async def discard_cached_result(self, result: ResourceFetchResult) -> None:
        self.discarded.append(result)

    def release_cached_result(self, result: ResourceFetchResult) -> None:
        self.released.append(result)


def query_for(role: Role = Role.OWNER) -> FactQuery:
    scope = DataScope(
        TenantId("synthetic-tenant"),
        frozenset({EmployeeId("self-worker")}),
        frozenset({DeptId("authorized-dept")}),
        NOW,
        ScopeVersion("scope-1"),
        role is Role.OWNER,
    )
    return FactQuery(
        "reported_production",
        ("quantity", "business_date", "bed_code"),
        NarrowedFilters(
            scope.tenant_id,
            scope.employee_ids if role is Role.EMPLOYEE else None,
            scope.dept_ids,
            bed_codes=frozenset({"01555"}),
        ),
        scope,
        role,
        WINDOW,
    )


def raw_result(*rows: dict[str, object]) -> ResourceFetchResult:
    return ResourceFetchResult(tuple(rows), len(rows), 1, True, source_call_count=1)


@pytest.mark.parametrize(
    "fault", ["tenant", "employee", "missing_employee", "requested_department"]
)
@pytest.mark.asyncio
async def test_fact_source_rejects_untrusted_scope_before_executor_calls(fault: str) -> None:
    query = query_for(Role.EMPLOYEE)
    altered = {
        "tenant": replace(query.filters, tenant_id=TenantId("another-tenant")),
        "employee": replace(query.filters, employee_ids=frozenset({EmployeeId("outside-worker")})),
        "missing_employee": replace(query.filters, employee_ids=None),
        "requested_department": replace(
            query.filters, requested_dept_ids=frozenset({DeptId("outside-dept")})
        ),
    }[fault]
    executor = RecordingExecutor(raw_result())
    with pytest.raises(ForbiddenError):
        await CachedFactSource(executor, FrozenClock(NOW)).fetch(replace(query, filters=altered))
    assert executor.calls == []


@pytest.mark.asyncio
async def test_fact_source_employee_projection_excludes_outside_identity_before_return() -> None:
    executor = RecordingExecutor(
        raw_result(
            {"uid": "self-worker", "inputdate": "2026-09-10", "sl": "0", "chuanghao": "01555"},
            {"uid": "outside-worker", "inputdate": "2026-09-10", "sl": "999", "chuanghao": "01555"},
        )
    )
    batch = await CachedFactSource(executor, FrozenClock(NOW)).fetch(query_for(Role.EMPLOYEE))
    assert len(batch.rows) == 1
    assert batch.rows[0]["quantity"] == Decimal(0)
    assert "uid" not in batch.rows[0]
    assert "outside-worker" not in str(batch.rows)
    assert batch.missing_fields == batch.invalid_fields == ()
    assert len(executor.calls) == 1
    request = executor.calls[0][1]
    assert request.allow_owner_snapshot is False
    assert request.result_validator is not None
    assert request.result_validator(executor.result)


@pytest.mark.parametrize(
    "row,missing,invalid",
    [
        ({"inputdate": "2026-09-10", "chuanghao": "01555"}, ("quantity",), ()),
        ({"sl": "1", "chuanghao": "01555"}, ("business_date",), ()),
        ({"sl": "NaN", "inputdate": "2026-09-10", "chuanghao": "01555"}, (), ("quantity",)),
        ({"sl": "0.123456789", "inputdate": "2026-09-10", "chuanghao": "01555"}, (), ("quantity",)),
        (
            {"sl": "100000000000000000000", "inputdate": "2026-09-10", "chuanghao": "01555"},
            (),
            ("quantity",),
        ),
        ({"sl": "1", "inputdate": "2026-02-30", "chuanghao": "01555"}, (), ("business_date",)),
    ],
)
@pytest.mark.asyncio
async def test_required_numeric_and_date_fields_never_receive_fabricated_defaults(
    row: dict[str, object], missing: tuple[str, ...], invalid: tuple[str, ...]
) -> None:
    executor = RecordingExecutor(raw_result(row))
    batch = await CachedFactSource(executor, FrozenClock(NOW)).fetch(query_for())
    assert batch.missing_fields == missing
    assert batch.invalid_fields == invalid
    for field in (*missing, *invalid):
        assert field not in batch.rows[0]
    request = executor.calls[0][1]
    assert request.result_validator is not None
    assert request.result_validator(executor.result) is False


@pytest.mark.asyncio
async def test_utc_payroll_timestamp_is_normalized_to_factory_calendar_day() -> None:
    executor = RecordingExecutor(
        raw_result({"inputdate": "2026-09-30T20:00:00Z", "sl": "2.50", "chuanghao": "01555"})
    )
    batch = await CachedFactSource(
        executor, FrozenClock(NOW), factory_timezone="Asia/Shanghai"
    ).fetch(query_for())
    assert batch.rows[0]["business_date"] == "2026-10-01"
    assert batch.rows[0]["quantity"] == Decimal("2.50")
    assert batch.generated_at == NOW


@pytest.mark.parametrize("consistent", [True, False])
@pytest.mark.asyncio
async def test_shadow_mismatch_discards_cache_and_both_paths_return_validated_live_data(
    consistent: bool,
) -> None:
    live = raw_result({"inputdate": "2026-09-10", "sl": "3", "chuanghao": "01555"})
    cached = raw_result(
        {"inputdate": "2026-09-10", "sl": "3" if consistent else "2", "chuanghao": "01555"}
    )
    served = replace(cached, shadow_result=live, cache_source="precompute", source_call_count=2)
    executor = RecordingExecutor(served)
    batch = await CachedFactSource(executor, FrozenClock(NOW)).fetch(query_for())
    assert batch.rows[0]["quantity"] == Decimal(3)
    assert batch.cache_source == "live"
    assert batch.source_calls == 2
    assert batch.complete
    assert executor.discarded == ([] if consistent else [served])
    assert executor.released == ([served] if consistent else [])


@pytest.mark.asyncio
async def test_invalid_shadow_is_discarded_and_remains_incomplete_without_zero_quantity() -> None:
    live = raw_result({"inputdate": "2026-09-10", "chuanghao": "01555"})
    cached = raw_result({"inputdate": "2026-09-10", "sl": "2", "chuanghao": "01555"})
    served = replace(cached, shadow_result=live, cache_source="precompute", source_call_count=1)
    executor = RecordingExecutor(served)
    batch = await CachedFactSource(executor, FrozenClock(NOW)).fetch(query_for())
    assert batch.complete is False
    assert batch.missing_fields == ("quantity",)
    assert "quantity" not in batch.rows[0]
    assert executor.discarded == [served]
    assert executor.released == []
