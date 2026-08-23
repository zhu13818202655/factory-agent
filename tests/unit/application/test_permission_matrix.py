"""Story 2: FR-001~FR-012 role-capability matrix tests, including denials."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.permission_matrix import (
    EMPLOYEE_CAPABILITIES,
    MANAGER_CAPABILITIES,
    OWNER_CAPABILITIES,
    ROLE_CAPABILITIES,
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
)

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)

ALL_CAPABILITIES = sorted(item.value for item in Capability)


def make_context(role: Role) -> TenantContext:
    from factory_agent.domain import UserId

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


def test_matrix_covers_all_twelve_capabilities() -> None:
    assigned = {
        capability for capabilities in ROLE_CAPABILITIES.values() for capability in capabilities
    }

    assert {item.value for item in assigned} == set(ALL_CAPABILITIES)


@pytest.mark.parametrize("capability", sorted(EMPLOYEE_CAPABILITIES, key=str))
def test_employee_allowed_capabilities(capability: Capability) -> None:
    decision = authorize_capability(capability, make_context(Role.EMPLOYEE), scope())

    assert decision.allowed


@pytest.mark.parametrize(
    "capability",
    sorted(OWNER_CAPABILITIES | (MANAGER_CAPABILITIES - EMPLOYEE_CAPABILITIES), key=str),
)
def test_employee_denied_capabilities_include_available_list(
    capability: Capability,
) -> None:
    decision = authorize_capability(capability, make_context(Role.EMPLOYEE), scope())

    assert not decision.allowed
    assert decision.available_capabilities == tuple(
        sorted(item.value for item in EMPLOYEE_CAPABILITIES)
    )


@pytest.mark.parametrize("capability", sorted(MANAGER_CAPABILITIES, key=str))
def test_manager_allowed_capabilities(capability: Capability) -> None:
    decision = authorize_capability(capability, make_context(Role.MANAGER), scope())

    assert decision.allowed


@pytest.mark.parametrize(
    "capability",
    sorted(OWNER_CAPABILITIES | (EMPLOYEE_CAPABILITIES - MANAGER_CAPABILITIES), key=str),
)
def test_manager_denied_capabilities(capability: Capability) -> None:
    decision = authorize_capability(capability, make_context(Role.MANAGER), scope())

    assert not decision.allowed


@pytest.mark.parametrize("capability", sorted(OWNER_CAPABILITIES, key=str))
def test_owner_allowed_capabilities(capability: Capability) -> None:
    decision = authorize_capability(capability, make_context(Role.OWNER), scope())

    assert decision.allowed


@pytest.mark.parametrize(
    "capability",
    sorted(EMPLOYEE_CAPABILITIES | MANAGER_CAPABILITIES - OWNER_CAPABILITIES, key=str),
)
def test_owner_denied_capabilities(capability: Capability) -> None:
    decision = authorize_capability(capability, make_context(Role.OWNER), scope())

    assert not decision.allowed


def test_scope_tenant_mismatch_is_denied_for_every_role() -> None:
    foreign = scope("tenant-b")

    for role in Role:
        decision = authorize_capability(Capability.OWN_OUTPUT, make_context(role), foreign)

        assert not decision.allowed
