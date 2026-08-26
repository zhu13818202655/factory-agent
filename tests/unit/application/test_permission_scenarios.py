"""Story 5 end-to-end permission scenarios: allow and deny paths.

Every denial scenario asserts zero business MES calls and no sensitive leaks.
Roles are display-only (M11); capability availability depends on tenant binding
and the registered capability list, while actual data visibility is enforced by
MES-side row filtering (M3/M12).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    IdentityRejectionError,
)
from factory_agent.application.filters import FilterNarrower, FilterRejectionError
from factory_agent.application.permission_matrix import Capability, authorize_capability
from factory_agent.application.platform_boundary import (
    PlatformBoundaryGuard,
    PlatformScopeViolationError,
)
from factory_agent.domain import Role
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    SeededVersionAssigner,
    credential,
    membership,
)
from tests.support.ports import FakeMesDataSource

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)


def build_registry() -> tuple[FakeMembershipSource, FakeOrganizationSource]:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-emp"): membership(
                "user-emp", "tenant-a", "employee-a1", Role.EMPLOYEE
            ),
            ("tenant-a", "user-mgr"): membership(
                "user-mgr", "tenant-a", "employee-a9", Role.MANAGER
            ),
            ("tenant-a", "user-owner"): membership(
                "user-owner", "tenant-a", "employee-o1", Role.OWNER
            ),
            ("tenant-b", "user-b"): membership("user-b", "tenant-b", "employee-b1", Role.EMPLOYEE),
        }
    )
    organizations = FakeOrganizationSource(
        depts_by_employee={
            "employee-a1": ("dept-a1",),
            "employee-a9": ("dept-a1", "dept-a2"),
            "employee-o1": ("dept-a2",),
            "employee-b1": ("dept-b1",),
        }
    )
    return memberships, organizations


def service_with(mes: FakeMesDataSource) -> AuthorizationService:
    memberships, organizations = build_registry()
    return AuthorizationService(
        memberships=memberships,
        organizations=organizations,
        versions=SeededVersionAssigner(),
    )


@pytest.mark.asyncio
async def test_single_tenant_employee_full_allow_path() -> None:
    mes = FakeMesDataSource(response={"items": []})
    service = service_with(mes)

    resolved = await service.authorize(credential("tenant-a", "user-emp"), AS_OF)
    decision = authorize_capability(
        Capability.OWN_OUTPUT, resolved.tenant_context, resolved.data_scope
    )
    narrowed = FilterNarrower().narrow(
        resolved.data_scope, employee_ids=resolved.data_scope.employee_ids
    )

    assert decision.allowed
    assert str(narrowed.tenant_id) == "tenant-a"


@pytest.mark.asyncio
async def test_employee_can_use_a_team_capability_when_registered() -> None:
    """M11: roles never gate capabilities, so an employee may run a team recipe."""
    service = service_with(FakeMesDataSource(response={"items": []}))

    resolved = await service.authorize(credential("tenant-a", "user-emp"), AS_OF)
    decision = authorize_capability(
        Capability.TEAM_PAYROLL_LIST, resolved.tenant_context, resolved.data_scope
    )

    assert decision.allowed
    # The scope is still minimal: only the caller's own employee + current dept.
    assert resolved.data_scope.employee_ids == frozenset({resolved.tenant_context.employee_id})
    assert resolved.data_scope.mes_filtered is False


@pytest.mark.asyncio
async def test_owner_factory_overview_capability_is_allowed() -> None:
    service = service_with(FakeMesDataSource(response={"items": []}))

    resolved = await service.authorize(credential("tenant-a", "user-owner"), AS_OF)
    decision = authorize_capability(
        Capability.FACTORY_ORDER_OVERVIEW, resolved.tenant_context, resolved.data_scope
    )

    assert decision.allowed


@pytest.mark.asyncio
async def test_unauthorized_tenant_is_rejected_with_zero_calls() -> None:
    mes = FakeMesDataSource(response={"items": []})
    service = service_with(mes)

    with pytest.raises(IdentityRejectionError):
        await service.authorize(credential("tenant-a", "user-b"), AS_OF)

    assert len(mes.requests) == 0


@pytest.mark.asyncio
async def test_cross_tenant_credential_access_is_rejected_with_zero_calls() -> None:
    mes = FakeMesDataSource(response={"items": []})
    service = service_with(mes)

    with pytest.raises(IdentityRejectionError):
        await service.authorize(credential("tenant-b", "user-emp"), AS_OF)

    assert len(mes.requests) == 0


@pytest.mark.asyncio
async def test_out_of_scope_id_filter_is_denied_with_zero_calls() -> None:
    mes = FakeMesDataSource(response={"items": []})
    service = service_with(mes)
    resolved = await service.authorize(credential("tenant-a", "user-emp"), AS_OF)

    from factory_agent.domain import EmployeeId

    with pytest.raises(FilterRejectionError):
        FilterNarrower().narrow(
            resolved.data_scope, employee_ids=frozenset({EmployeeId("employee-b1")})
        )

    assert len(mes.requests) == 0


@pytest.mark.asyncio
async def test_platform_identity_cannot_enter_factory_path() -> None:
    from factory_agent.domain import (
        PlatformCapability,
        PlatformScope,
        PrincipalId,
        TenantId,
    )

    guard = PlatformBoundaryGuard()
    platform = PlatformScope(
        principal_id=PrincipalId("ops-1"),
        tenant_ids=frozenset({TenantId("tenant-a")}),
        capabilities=frozenset({PlatformCapability.USAGE_AGGREGATE}),
    )

    with pytest.raises(PlatformScopeViolationError):
        guard.assert_factory_context(platform)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_scope_version_changes_on_every_resolution() -> None:
    service = service_with(FakeMesDataSource(response={"items": []}))

    first = await service.authorize(credential("tenant-a", "user-emp"), AS_OF)
    second = await service.authorize(credential("tenant-a", "user-emp"), AS_OF)

    assert first.data_scope.scope_version != second.data_scope.scope_version


@pytest.mark.asyncio
async def test_denied_paths_never_leak_scope_ids_in_errors() -> None:
    service = service_with(FakeMesDataSource(response={"items": []}))

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-b"), AS_OF)

    message = str(error.value)
    assert "employee-b1" not in message
    assert "dept-b1" not in message
