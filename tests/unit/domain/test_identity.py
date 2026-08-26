from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    MembershipId,
    PlatformCapability,
    PlatformScope,
    PrincipalId,
    Role,
    ScopeVersion,
    TenantId,
)


def _scope(
    employee_ids: frozenset[EmployeeId] | None = None,
    dept_ids: frozenset[DeptId] | None = None,
    *,
    mes_filtered: bool = False,
) -> DataScope:
    return DataScope(
        tenant_id=TenantId("tenant-a"),
        employee_ids=employee_ids or frozenset(),
        dept_ids=dept_ids or frozenset(),
        evaluated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        scope_version=ScopeVersion("scope-v1"),
        mes_filtered=mes_filtered,
    )


def _employees(*values: str) -> frozenset[EmployeeId]:
    return frozenset(EmployeeId(value) for value in values)


def _depts(*values: str) -> frozenset[DeptId]:
    return frozenset(DeptId(value) for value in values)


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        (EmployeeId, ""),
        (EmployeeId, " employee-1"),
        (DeptId, "dept-1 "),
        (MembershipId, ""),
        (PrincipalId, ""),
        (ScopeVersion, "  "),
    ],
)
def test_identity_ids_reject_empty_or_untrimmed_values(value_type: type, value: str) -> None:
    with pytest.raises(ValueError):
        value_type(value)


def test_membership_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        Role("superuser")


def test_data_scope_is_immutable() -> None:
    scope = _scope(_employees("employee-1"), _depts("group-a1"))

    with pytest.raises(FrozenInstanceError):
        scope.employee_ids = frozenset()  # type: ignore[misc]


def test_data_scope_has_no_broadening_api() -> None:
    public_names = {name for name in dir(DataScope) if not name.startswith("_")}

    assert public_names == {
        "dept_ids",
        "employee_ids",
        "evaluated_at",
        "is_whole_tenant",
        "mes_filtered",
        "narrow_to_depts",
        "narrow_to_employees",
        "scope_version",
        "tenant_id",
    }


def test_data_scope_narrowing_intersects_and_never_unions() -> None:
    scope = _scope(_employees("e1", "e2"), _depts("g1", "g2"))

    narrowed = scope.narrow_to_employees(_employees("e2", "e9"))
    assert narrowed is not None
    assert narrowed.employee_ids == _employees("e2")

    narrowed_depts = scope.narrow_to_depts(_depts("g2", "g9"))
    assert narrowed_depts is not None
    assert narrowed_depts.dept_ids == _depts("g2")


def test_data_scope_narrowing_cannot_add_ids() -> None:
    scope = _scope(_employees("e1"), _depts("g1"))

    outside = scope.narrow_to_employees(_employees("e2"))
    assert outside is None


def test_data_scope_is_never_whole_tenant_in_story_5() -> None:
    """M3/M12: there is no locally proven whole-tenant scope."""
    scope = _scope(_employees("e1"), _depts("g1"))

    assert scope.is_whole_tenant() is False
    # Even a wide scope does not claim whole-tenant visibility.
    assert _scope(_employees("e1", "e2"), _depts("g1", "g2")).is_whole_tenant() is False


def test_mes_filtered_is_preserved_through_narrowing_and_not_broadened() -> None:
    scope = _scope(_employees("e1", "e2"), _depts("g1"), mes_filtered=True)

    narrowed = scope.narrow_to_employees(_employees("e2"))
    assert narrowed is not None
    assert narrowed.mes_filtered is True
    # Narrowing can never turn a non-filtered scope into a filtered one.
    plain = _scope(_employees("e1"), _depts("g1"), mes_filtered=False)
    plain_narrowed = plain.narrow_to_employees(_employees("e1"))
    assert plain_narrowed is not None
    assert plain_narrowed.mes_filtered is False


def test_platform_scope_is_independent_of_data_scope() -> None:
    platform = PlatformScope(
        principal_id=PrincipalId("ops-1"),
        tenant_ids=frozenset({TenantId("tenant-a")}),
        capabilities=frozenset({PlatformCapability.USAGE_AGGREGATE}),
    )
    assert isinstance(platform, PlatformScope)
    assert not isinstance(platform, DataScope)
    assert platform.allows(PlatformCapability.USAGE_AGGREGATE)
    assert not platform.allows(PlatformCapability.USAGE_REPORT)
    assert platform.covers(TenantId("tenant-a"))
    assert not platform.covers(TenantId("tenant-b"))

    with pytest.raises(FrozenInstanceError):
        platform.capabilities = frozenset()  # type: ignore[misc]
