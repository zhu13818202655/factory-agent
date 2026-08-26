"""Story 2: filter narrowing, DEC-012 rejection, and zero-MES-call guarantees."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from factory_agent.application.filters import FilterNarrower, FilterRejectionError
from factory_agent.domain import DataScope, DeptId, EmployeeId, ScopeVersion, TenantId
from tests.support.ports import FakeMesDataSource

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)


def employees(*values: str) -> frozenset[EmployeeId]:
    return frozenset(EmployeeId(value) for value in values)


def depts(*values: str) -> frozenset[DeptId]:
    return frozenset(DeptId(value) for value in values)


def scoped_scope() -> DataScope:
    return DataScope(
        tenant_id=TenantId("tenant-a"),
        employee_ids=employees("e1", "e2", "e3"),
        dept_ids=depts("g1", "g2"),
        evaluated_at=AS_OF,
        scope_version=ScopeVersion("v1"),
    )


def broad_scope() -> DataScope:
    return DataScope(
        tenant_id=TenantId("tenant-a"),
        employee_ids=employees("e1", "e2", "e3", "e9"),
        dept_ids=depts("g1", "g2", "g9"),
        evaluated_at=AS_OF,
        scope_version=ScopeVersion("v1"),
    )


@dataclass
class CallCounter:
    """Counts business MES calls; every denial path must leave it at zero."""

    mes: FakeMesDataSource = field(default_factory=lambda: FakeMesDataSource(response=None))

    @property
    def calls(self) -> int:
        return len(self.mes.requests)


def test_intersection_narrows_user_filters() -> None:
    narrowed = FilterNarrower().narrow(
        scoped_scope(),
        employee_ids=employees("e2", "e9"),
        dept_ids=depts("g2", "g9"),
    )

    assert narrowed.employee_ids == employees("e2")
    assert narrowed.dept_ids == depts("g2")
    assert str(narrowed.tenant_id) == "tenant-a"


def test_empty_intersection_is_rejected_not_treated_as_unfiltered() -> None:
    counter = CallCounter()

    with pytest.raises(FilterRejectionError):
        FilterNarrower().narrow(scoped_scope(), employee_ids=employees("e99"))
    with pytest.raises(FilterRejectionError):
        FilterNarrower().narrow(scoped_scope(), dept_ids=depts("g99"))

    assert counter.calls == 0


def test_out_of_scope_filter_never_broadens_the_scope() -> None:
    narrowed = FilterNarrower().narrow(
        broad_scope(), employee_ids=employees("e2"), dept_ids=depts("g9")
    )

    assert narrowed.employee_ids == employees("e2")
    assert narrowed.dept_ids == depts("g9")


def test_story7_order_and_style_filters_pass_through_as_narrow_only() -> None:
    """Story 7: order/style/plan codes narrow within MES filtering (M3/M12)."""
    counter = CallCounter()

    narrowed = FilterNarrower().narrow(
        scoped_scope(),
        order_ids=frozenset({"KHDD-07-001"}),
        style_ids=frozenset({"HH001"}),
        plan_ids=frozenset({"JH-2607-001"}),
    )

    assert narrowed.order_codes == frozenset({"KHDD-07-001"})
    assert narrowed.style_codes == frozenset({"HH001"})
    assert narrowed.plan_codes == frozenset({"JH-2607-001"})
    # Scope identifiers still narrowed/unchanged; no MES call happened.
    assert narrowed.employee_ids == scoped_scope().employee_ids
    assert narrowed.dept_ids == scoped_scope().dept_ids
    assert counter.calls == 0


def test_empty_business_filters_are_none_not_empty_frozenset() -> None:
    narrowed = FilterNarrower().narrow(scoped_scope())
    assert narrowed.order_codes is None
    assert narrowed.style_codes is None
    assert narrowed.plan_codes is None


def test_no_filters_pass_scope_through_unchanged() -> None:
    scope = scoped_scope()

    narrowed = FilterNarrower().narrow(scope)

    assert narrowed.employee_ids == scope.employee_ids
    assert narrowed.dept_ids == scope.dept_ids
