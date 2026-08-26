from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    PlatformCapability,
    PlatformScope,
    PrincipalId,
    ScopeVersion,
    TenantId,
)
from factory_agent.domain.errors import ForbiddenError
from factory_agent.execution.executor import ExecutionRequest
from tests.support.execution_kernel import make_scoped_executor


def _scope(
    tenant: str = "tenant-a",
    employees: tuple[str, ...] = ("employee-a1",),
    depts: tuple[str, ...] = ("group-a1",),
) -> DataScope:
    return DataScope(
        tenant_id=TenantId(tenant),
        employee_ids=frozenset(EmployeeId(e) for e in employees),
        dept_ids=frozenset(DeptId(d) for d in depts),
        evaluated_at=datetime(2026, 8, 21, tzinfo=UTC),
        scope_version=ScopeVersion("scope-test"),
    )


def _filters(
    tenant: str = "tenant-a",
    employees: tuple[str, ...] | None = ("employee-a1",),
    depts: tuple[str, ...] | None = ("group-a1",),
) -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId(tenant),
        employee_ids=frozenset(EmployeeId(e) for e in employees) if employees else None,
        dept_ids=frozenset(DeptId(d) for d in depts) if depts else None,
    )


def _time_range() -> tuple[datetime, datetime]:
    return (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_scope_parameters_flow_only_from_narrowed_filters() -> None:
    adapter, executor = make_scoped_executor()
    result = await executor.execute_step(
        _filters(),
        ExecutionRequest(operation_id="YskQuery", time_range=_time_range()),
        active_scope=_scope(),
    )
    assert len(adapter.requests) == 1
    operation_id, filters, time_range, page_size = adapter.requests[0]
    assert operation_id == "YskQuery"
    assert filters.tenant_id == TenantId("tenant-a")
    assert filters.employee_ids is not None and {str(item) for item in filters.employee_ids} == {
        "employee-a1"
    }
    assert filters.dept_ids is not None and {str(item) for item in filters.dept_ids} == {"group-a1"}
    assert time_range == _time_range()
    assert page_size == 50
    assert result.complete is True


@pytest.mark.asyncio
async def test_out_of_scope_employee_id_never_reaches_adapter() -> None:
    adapter, executor = make_scoped_executor()
    with pytest.raises(ForbiddenError):
        await executor.execute_step(
            _filters(employees=("employee-other",)),
            ExecutionRequest(operation_id="YskQuery", time_range=_time_range()),
            active_scope=_scope(),
        )
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_tenant_rewrite_is_rejected_with_zero_calls() -> None:
    adapter, executor = make_scoped_executor()
    with pytest.raises(ForbiddenError):
        await executor.execute_step(
            _filters(tenant="tenant-b"),
            ExecutionRequest(operation_id="YskQuery", time_range=_time_range()),
            active_scope=_scope(),
        )
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_dept_escalation_is_rejected_with_zero_calls() -> None:
    adapter, executor = make_scoped_executor()
    with pytest.raises(ForbiddenError):
        await executor.execute_step(
            _filters(depts=("workshop-b1",)),
            ExecutionRequest(operation_id="YskQuery", time_range=_time_range()),
            active_scope=_scope(),
        )
    assert adapter.requests == []


def test_platform_scope_never_enters_mes_execution() -> None:
    from factory_agent.execution.executor import reject_platform_scope

    platform = PlatformScope(
        principal_id=PrincipalId("platform-admin"),
        tenant_ids=frozenset({TenantId("tenant-a")}),
        capabilities=frozenset({PlatformCapability.USAGE_AGGREGATE}),
    )
    with pytest.raises(ForbiddenError):
        reject_platform_scope(platform)


@pytest.mark.asyncio
async def test_unregistered_operation_is_rejected_before_http() -> None:
    from factory_agent.domain.errors import UnsupportedOperationError

    adapter, executor = make_scoped_executor()
    with pytest.raises(UnsupportedOperationError):
        await executor.execute_step(
            _filters(),
            ExecutionRequest(operation_id="X9_bad", time_range=_time_range()),
        )
    assert adapter.requests == []
