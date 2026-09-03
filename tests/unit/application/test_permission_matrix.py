"""Capability-role matrix tests (role is authoritative).

The MES token ``roles`` code gates capability availability through the reviewed
matrix (``docs/product/需求及方案整理.md`` 功能表): personal capabilities
FR-001..FR-004 are available to all four roles, management capabilities
FR-005..FR-008 to group leaders and managers, and factory-wide capabilities
FR-009..FR-012 to the owner. Data visibility inside an allowed capability is
still enforced by MES-side row filtering (``DataScope.mes_filtered``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.permission_matrix import (
    CAPABILITY_ROLES,
    REGISTERED_CAPABILITIES,
    Capability,
    authorize_capability,
    capabilities_for_role,
)
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    Role,
    ScopeVersion,
    TenantContext,
    TenantId,
    UserId,
)

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)

ALL_ROLES = frozenset(Role)
MANAGEMENT_ROLES = frozenset({Role.GROUP_LEADER, Role.MANAGER})
OWNER_ROLES = frozenset({Role.OWNER})

EXPECTED_MATRIX: dict[Capability, frozenset[Role]] = {
    Capability.OWN_OUTPUT: ALL_ROLES,
    Capability.OWN_PAYROLL_SUMMARY: ALL_ROLES,
    Capability.OWN_PAYROLL_DETAIL: ALL_ROLES,
    Capability.GROUP_INCOME_RANK: ALL_ROLES,
    Capability.ORDER_PROGRESS: MANAGEMENT_ROLES,
    Capability.ORDER_OUTPUT: MANAGEMENT_ROLES,
    Capability.WORKSHOP_COMPARISON: MANAGEMENT_ROLES,
    Capability.TEAM_PAYROLL_LIST: MANAGEMENT_ROLES,
    Capability.FACTORY_ORDER_OVERVIEW: OWNER_ROLES,
    Capability.WORKSHOP_OUTPUT_OVERVIEW: OWNER_ROLES,
    Capability.FACTORY_PAYROLL_STATS: OWNER_ROLES,
    Capability.ANY_EMPLOYEE_PAYROLL: OWNER_ROLES,
}


def make_context(role: Role) -> TenantContext:
    return TenantContext(
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-x"),
        employee_id=EmployeeId("employee-x"),
        role=role,
        resolved_at=AS_OF,
    )


def scope(tenant_id: str = "tenant-a") -> DataScope:
    return DataScope(
        tenant_id=TenantId(tenant_id),
        employee_ids=frozenset({EmployeeId("employee-x")}),
        dept_ids=frozenset({DeptId("group-a1")}),
        evaluated_at=AS_OF,
        scope_version=ScopeVersion("v"),
    )


def test_registered_capabilities_cover_the_twelve_l1_capabilities() -> None:
    assert len(REGISTERED_CAPABILITIES) == 12
    assert set(item.value for item in REGISTERED_CAPABILITIES) == {
        "FR-001",
        "FR-002",
        "FR-003",
        "FR-004",
        "FR-005",
        "FR-006",
        "FR-007",
        "FR-008",
        "FR-009",
        "FR-010",
        "FR-011",
        "FR-012",
    }


def test_reviewed_matrix_matches_the_product_function_tables() -> None:
    assert CAPABILITY_ROLES == EXPECTED_MATRIX


@pytest.mark.parametrize("capability", sorted(REGISTERED_CAPABILITIES, key=str))
def test_capability_is_allowed_exactly_for_its_matrix_roles(capability: Capability) -> None:
    allowed_roles = EXPECTED_MATRIX[capability]
    for role in Role:
        decision = authorize_capability(capability, make_context(role), scope())
        if role in allowed_roles:
            assert decision.allowed, (capability, role)
        else:
            assert not decision.allowed, (capability, role)
            assert decision.reason == "capability is not available for the current role"


def test_denied_decision_lists_the_role_available_capabilities() -> None:
    decision = authorize_capability(
        Capability.FACTORY_PAYROLL_STATS, make_context(Role.EMPLOYEE), scope()
    )
    assert not decision.allowed
    # An employee can only use the personal capabilities.
    assert set(decision.available_capabilities) == {"FR-001", "FR-002", "FR-003", "FR-004"}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Role.EMPLOYEE, {"FR-001", "FR-002", "FR-003", "FR-004"}),
        (
            Role.GROUP_LEADER,
            {"FR-001", "FR-002", "FR-003", "FR-004", "FR-005", "FR-006", "FR-007", "FR-008"},
        ),
        (
            Role.MANAGER,
            {"FR-001", "FR-002", "FR-003", "FR-004", "FR-005", "FR-006", "FR-007", "FR-008"},
        ),
        (
            Role.OWNER,
            {"FR-001", "FR-002", "FR-003", "FR-004", "FR-009", "FR-010", "FR-011", "FR-012"},
        ),
    ],
)
def test_capabilities_for_role(role: Role, expected: set[str]) -> None:
    assert {item.value for item in capabilities_for_role(role)} == expected


def test_tenant_mismatch_is_denied_for_every_role() -> None:
    foreign = scope("tenant-b")

    for role in Role:
        decision = authorize_capability(Capability.OWN_OUTPUT, make_context(role), foreign)

        assert not decision.allowed


def test_unavailable_metrics_are_never_rendered_as_numbers() -> None:
    """Unconfirmed metrics must never be rendered as fabricated numbers."""
    from factory_agent.execution.result_table import default_metric_registry

    registry = default_metric_registry()
    gaps = {
        "quality_defective": "unavailable-defective-v1",
        "plan_target_output": "unavailable-target-v1",
    }
    for name, version in gaps.items():
        resolved = registry.resolve(name, version)
        assert resolved.allows_numeric_rendering() is False


def test_closed_metrics_are_confirmed_and_renderable() -> None:
    """Story 1 口径关闭：在册人数与人均工资转为已确认口径。"""
    from factory_agent.execution.result_table import default_metric_registry

    registry = default_metric_registry()
    for name, version in {
        "org_headcount": "employee-registered-v1",
        "payroll_avg_by_dept": "customer-payroll-avg-v1",
        "time_flag_default": "confirmed-flag-v1",
    }.items():
        resolved = registry.resolve(name, version)
        assert resolved.allows_numeric_rendering() is True
