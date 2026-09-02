"""Capability-availability matrix tests (roles are display-only).

Roles never gate capabilities (M11/A.1). Every registered capability is
available to any authenticated caller; data visibility is enforced by MES-side
row filtering recorded in ``DataScope.mes_filtered``. Availability is decided
by the registered capability registry plus tenant binding.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.permission_matrix import (
    REGISTERED_CAPABILITIES,
    Capability,
    authorize_capability,
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


def test_registered_capabilities_cover_the_eleven_l1_capabilities() -> None:
    """FR-004 cancelled per M7; the remaining L1 set is what we register."""
    assert len(REGISTERED_CAPABILITIES) == 11
    assert not any(item.value == "FR-004" for item in REGISTERED_CAPABILITIES)
    assert set(item.value for item in REGISTERED_CAPABILITIES) == {
        "FR-001",
        "FR-002",
        "FR-003",
        "FR-005",
        "FR-006",
        "FR-007",
        "FR-008",
        "FR-009",
        "FR-010",
        "FR-011",
        "FR-012",
    }


@pytest.mark.parametrize("capability", sorted(REGISTERED_CAPABILITIES, key=str))
def test_every_registered_capability_is_allowed_for_any_role(capability: Capability) -> None:
    for role in Role:
        decision = authorize_capability(capability, make_context(role), scope())
        assert decision.allowed, (capability, role)


def test_tenant_mismatch_is_denied_for_every_role() -> None:
    foreign = scope("tenant-b")

    for role in Role:
        decision = authorize_capability(Capability.OWN_OUTPUT, make_context(role), foreign)

        assert not decision.allowed


def test_incomplete_story_5_output_columns_are_unavailable_not_numbers() -> None:
    """Unconfirmed metrics must never be rendered as fabricated numbers."""
    from factory_agent.execution.result_table import default_metric_registry

    registry = default_metric_registry()
    gaps = {
        "quality_defective": "unavailable-c5",
        "plan_target_output": "unavailable-c9",
        "org_headcount": "unavailable-c7",
        "production_stage": "unavailable-c8",
    }
    for name, version in gaps.items():
        resolved = registry.resolve(name, version)
        assert resolved.allows_numeric_rendering() is False
