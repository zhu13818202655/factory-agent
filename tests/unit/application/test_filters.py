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


def whole_scope() -> DataScope:
    return DataScope.whole_tenant(TenantId("tenant-a"), AS_OF, ScopeVersion("v1"))


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


def test_out_of_scope_filter_never_broadens_whole_tenant_exit() -> None:
    narrowed = FilterNarrower().narrow(
        whole_scope(), employee_ids=employees("anyone"), dept_ids=depts("somewhere")
    )

    assert narrowed.employee_ids == employees("anyone")
    assert narrowed.dept_ids == depts("somewhere")


@pytest.mark.parametrize("explicit", ["order-1", None])
def test_dec_012_explicit_order_or_style_ids_are_rejected(explicit: str | None) -> None:
    counter = CallCounter()

    with pytest.raises(FilterRejectionError) as error:
        FilterNarrower().narrow(
            scoped_scope(),
            order_ids=frozenset({explicit}) if explicit else None,
            style_ids=frozenset({"style-1"}) if explicit is None else None,
        )

    assert error.value.code == "invalid_request"
    assert counter.calls == 0


def test_no_filters_pass_scope_through_unchanged() -> None:
    scope = scoped_scope()

    narrowed = FilterNarrower().narrow(scope)

    assert narrowed.employee_ids == scope.employee_ids
    assert narrowed.dept_ids == scope.dept_ids
