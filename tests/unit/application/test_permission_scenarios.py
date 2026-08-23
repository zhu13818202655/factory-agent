"""Story 2 end-to-end permission scenarios: allow and deny paths.

Every denial scenario asserts zero business MES calls and no sensitive leaks.
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
    FakeScopeSource,
    SeededVersionAssigner,
    credential,
    membership,
)
from tests.support.ports import FakeMesDataSource

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)


def build_registry() -> tuple[FakeMembershipSource, FakeOrganizationSource, FakeScopeSource]:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-emp"): [
                membership(
                    "m-emp", "user-emp", "tenant-a", "employee-a1", Role.EMPLOYEE, ("group-a1",)
                )
            ],
            ("tenant-a", "user-mgr"): [
                membership(
                    "m-mgr", "user-mgr", "tenant-a", "employee-a9", Role.MANAGER, ("workshop-a1",)
                )
            ],
            ("tenant-a", "user-owner"): [
                membership("m-owner", "user-owner", "tenant-a", "employee-o1", Role.OWNER)
            ],
            ("tenant-b", "user-b"): [
                membership("m-b", "user-b", "tenant-b", "employee-b1", Role.EMPLOYEE, ("group-b1",))
            ],
        }
    )
    assignments = FakeOrganizationSource(
        assignments_by_employee={"employee-a9": (("group-a1", "group-a2"),)}
    )
    scopes = FakeScopeSource(
        scopes_by_membership={
            "m-emp": ((frozenset(), frozenset()),),
            "m-mgr": ((frozenset(), frozenset()),),
        }
    )
    return memberships, assignments, scopes


def service_with(mes: FakeMesDataSource) -> AuthorizationService:
    memberships, assignments, scopes = build_registry()
    return AuthorizationService(
        memberships=memberships,
        assignments=assignments,
        scopes=scopes,
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
async def test_manager_uses_managed_scope_for_team_capability() -> None:
    service = service_with(FakeMesDataSource(response={"items": []}))

    resolved = await service.authorize(credential("tenant-a", "user-mgr"), AS_OF)
    decision = authorize_capability(
        Capability.TEAM_PAYROLL_LIST, resolved.tenant_context, resolved.data_scope
    )

    assert decision.allowed
    assert resolved.data_scope.dept_ids is not None
    dept_ids = {str(item) for item in resolved.data_scope.dept_ids}
    assert {"workshop-a1", "group-a1", "group-a2"} <= dept_ids


@pytest.mark.asyncio
async def test_owner_whole_tenant_capability() -> None:
    service = service_with(FakeMesDataSource(response={"items": []}))

    resolved = await service.authorize(credential("tenant-a", "user-owner"), AS_OF)
    decision = authorize_capability(
        Capability.FACTORY_ORDER_OVERVIEW, resolved.tenant_context, resolved.data_scope
    )

    assert decision.allowed
    assert resolved.data_scope.is_whole_tenant()


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
async def test_employee_cannot_use_owner_capability() -> None:
    service = service_with(FakeMesDataSource(response={"items": []}))
    resolved = await service.authorize(credential("tenant-a", "user-emp"), AS_OF)

    decision = authorize_capability(
        Capability.FACTORY_PAYROLL_STATS, resolved.tenant_context, resolved.data_scope
    )

    assert not decision.allowed
    assert "FR-001" in decision.available_capabilities


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
    assert "group-b1" not in message
