"""Business filter resolution: dept/employee name -> id narrowing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.business_filters import (
    BusinessFilterResolver,
    DeptRecord,
    DirectoryError,
    EmployeeRecord,
)
from factory_agent.domain import DataScope, DeptId, EmployeeId, IntentSlots, ScopeVersion, TenantId

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)


class FakeDirectory:
    def __init__(
        self,
        depts: tuple[DeptRecord, ...] = (),
        employees: tuple[EmployeeRecord, ...] = (),
    ) -> None:
        self._depts = depts
        self._employees = employees

    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]:
        return self._depts

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]:
        return self._employees


def _scope() -> DataScope:
    return DataScope(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId("01001")}),
        dept_ids=frozenset({DeptId("dept-a1")}),
        evaluated_at=AS_OF,
        scope_version=ScopeVersion("v1"),
    )


def _slots(
    *,
    time_range_start: datetime | None = None,
    time_range_end: datetime | None = None,
    time_expression: str | None = None,
    order_codes: tuple[str, ...] = (),
    plan_codes: tuple[str, ...] = (),
    style_codes: tuple[str, ...] = (),
    dept_names: tuple[str, ...] = (),
    employee_names: tuple[str, ...] = (),
) -> IntentSlots:
    return IntentSlots(
        time_range_start=time_range_start,
        time_range_end=time_range_end,
        time_expression=time_expression,
        order_codes=order_codes,
        plan_codes=plan_codes,
        style_codes=style_codes,
        dept_names=dept_names,
        employee_names=employee_names,
    )


@pytest.mark.asyncio
async def test_resolves_dept_and_employee_names() -> None:
    resolver = BusinessFilterResolver(
        FakeDirectory(
            depts=(DeptRecord("dept-a1", "一车间", "YCJ"),),
            employees=(EmployeeRecord("01001", "模拟员工甲", "MNYGJ"),),
        )
    )
    resolved = await resolver.resolve(
        _scope(),
        _slots(dept_names=("一车间",), employee_names=("模拟员工甲",), order_codes=("KHDD-1",)),
    )
    assert resolved.dept_ids == frozenset({DeptId("dept-a1")})
    assert resolved.employee_ids == frozenset({EmployeeId("01001")})
    assert resolved.order_codes == frozenset({"KHDD-1"})
    assert resolved.style_codes is None


@pytest.mark.asyncio
async def test_empty_slots_resolve_to_none() -> None:
    resolver = BusinessFilterResolver(FakeDirectory())
    resolved = await resolver.resolve(_scope(), _slots())
    assert resolved.is_empty() is True
    assert resolved.dept_ids is None
    assert resolved.employee_ids is None


@pytest.mark.asyncio
async def test_unknown_dept_raises_not_found() -> None:
    resolver = BusinessFilterResolver(FakeDirectory(depts=()))
    with pytest.raises(DirectoryError) as error:
        await resolver.resolve(_scope(), _slots(dept_names=("不存在的车间",)))
    assert error.value.code == "not_found"


@pytest.mark.asyncio
async def test_same_name_employees_raise_ambiguous() -> None:
    # FR-012: 同名员工追问稳定 uid，不用姓名关联。
    resolver = BusinessFilterResolver(
        FakeDirectory(
            employees=(
                EmployeeRecord("01001", "模拟员工甲", "MNYGJ"),
                EmployeeRecord("01002", "模拟员工甲", "MNYGJ"),
            )
        )
    )
    with pytest.raises(DirectoryError) as error:
        await resolver.resolve(_scope(), _slots(employee_names=("模拟员工甲",)))
    assert error.value.code == "ambiguous"
    assert "工号" in error.value.message
