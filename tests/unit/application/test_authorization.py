"""Story 5: trusted identity resolution and tenant context tests.

The A1/A2/A3 closure chain is replaced by the customer credential bundle
(M1/M4/M15). ``tenant_id`` is the plaintext AppKey and ``employee_id`` is the
token ``user``; one factory has one AppKey, so membership is naturally unique.
``DataScope`` is the minimal provable range with ``mes_filtered`` recorded at
the adapter boundary, never here. Roles are display-only (M11).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    IdentityErrorCode,
    IdentityRejectionError,
)
from factory_agent.domain import Role
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    SeededVersionAssigner,
    credential,
    membership,
)

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)


def build_service(
    memberships: FakeMembershipSource | None = None,
    organizations: FakeOrganizationSource | None = None,
) -> AuthorizationService:
    return AuthorizationService(
        memberships=memberships or FakeMembershipSource(),
        organizations=organizations
        or FakeOrganizationSource(depts_by_employee={"employee-a1": ("dept-a1",)}),
        versions=SeededVersionAssigner(),
    )


@pytest.mark.asyncio
async def test_unique_membership_resolves_identity_context_and_scope() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-a"): membership("user-a", "tenant-a", "employee-a1", Role.EMPLOYEE)
        }
    )
    service = build_service(memberships)

    resolved = await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert str(resolved.identity.tenant_id) == "tenant-a"
    assert str(resolved.identity.user_id) == "user-a"
    assert resolved.tenant_context.role is Role.EMPLOYEE
    assert resolved.tenant_context.resolved_at == AS_OF
    assert str(resolved.data_scope.scope_version) == "scope-00000001"
    assert resolved.data_scope.employee_ids == frozenset({resolved.tenant_context.employee_id})
    dept_ids = {str(item) for item in resolved.data_scope.dept_ids}
    assert dept_ids == {"dept-a1"}
    assert resolved.data_scope.mes_filtered is False


@pytest.mark.asyncio
async def test_unknown_credential_is_not_found_and_produces_no_context() -> None:
    service = build_service(FakeMembershipSource())

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-x", "user-x"), AS_OF)

    assert error.value.code is IdentityErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_credential_pair_mismatch_is_forbidden() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-a"): membership("user-a", "tenant-b", "employee-a1", Role.EMPLOYEE)
        }
    )
    service = build_service(memberships)

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert error.value.code is IdentityErrorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_cross_tenant_credential_is_isolated() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-b", "user-b"): membership("user-b", "tenant-b", "employee-b1", Role.EMPLOYEE)
        }
    )
    service = build_service(memberships)

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-b"), AS_OF)

    assert error.value.code is IdentityErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_missing_current_department_is_not_found() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-a"): membership("user-a", "tenant-a", "employee-a1", Role.EMPLOYEE)
        }
    )
    service = build_service(memberships, FakeOrganizationSource(depts_by_employee={}))

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert error.value.code is IdentityErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_role_is_display_only_so_scope_is_identical_across_roles() -> None:
    """M11: roles never gate the computed DataScope."""
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-a"): membership("user-a", "tenant-a", "employee-a1", Role.EMPLOYEE),
            ("tenant-a", "mgr"): membership("mgr", "tenant-a", "employee-m9", Role.MANAGER),
        }
    )
    organizations = FakeOrganizationSource(
        depts_by_employee={"employee-a1": ("dept-a1",), "employee-m9": ("dept-a1",)}
    )
    service = build_service(memberships, organizations)

    employee = await service.authorize(credential("tenant-a", "user-a"), AS_OF)
    manager = await service.authorize(credential("tenant-a", "mgr"), AS_OF)

    assert employee.tenant_context.role is Role.EMPLOYEE
    assert manager.tenant_context.role is Role.MANAGER
    # Both scopes are minimal: only their own employee and current departments.
    assert employee.data_scope.employee_ids == frozenset({employee.tenant_context.employee_id})
    assert manager.data_scope.employee_ids == frozenset({manager.tenant_context.employee_id})
    assert employee.data_scope.mes_filtered is manager.data_scope.mes_filtered is False
