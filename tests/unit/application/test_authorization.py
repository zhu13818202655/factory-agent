"""Story 2: trusted identity resolution and tenant context tests."""

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
    FakeScopeSource,
    SeededVersionAssigner,
    credential,
    membership,
)

AS_OF = datetime(2026, 8, 21, 8, tzinfo=timezone.utc)


def build_service(
    memberships: FakeMembershipSource | None = None,
) -> AuthorizationService:
    return AuthorizationService(
        memberships=memberships or FakeMembershipSource(),
        assignments=FakeOrganizationSource(),
        scopes=FakeScopeSource(),
        versions=SeededVersionAssigner(),
    )


@pytest.mark.asyncio
async def test_unique_membership_resolves_identity_context_and_scope() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-a"): [
                membership(
                    "membership-a",
                    "user-a",
                    "tenant-a",
                    "employee-a1",
                    Role.EMPLOYEE,
                    ("group-a1",),
                )
            ]
        }
    )
    scopes = FakeScopeSource(
        scopes_by_membership={
            "membership-a": ((frozenset(), frozenset()),),
        }
    )
    service = AuthorizationService(
        memberships=memberships,
        assignments=FakeOrganizationSource(),
        scopes=scopes,
        versions=SeededVersionAssigner(),
    )

    resolved = await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert str(resolved.identity.tenant_id) == "tenant-a"
    assert resolved.tenant_context.role is Role.EMPLOYEE
    assert resolved.tenant_context.resolved_at == AS_OF
    assert str(resolved.data_scope.scope_version) == "scope-00000001"
    assert resolved.data_scope.employee_ids is not None
    employee_ids = {str(item) for item in resolved.data_scope.employee_ids}
    assert employee_ids == {"employee-a1"}


@pytest.mark.asyncio
async def test_expired_membership_is_not_found_and_produces_no_context() -> None:
    expired = membership(
        "membership-old",
        "user-a",
        "tenant-a",
        "employee-a1",
        Role.EMPLOYEE,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    memberships = FakeMembershipSource(
        memberships_by_credential={("tenant-a", "user-a"): [expired]}
    )
    service = build_service(memberships)

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert error.value.code is IdentityErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_multiple_membership_hits_are_rejected_as_data_error() -> None:
    first = membership("m-1", "user-a", "tenant-a", "employee-a1", Role.EMPLOYEE)
    second = membership("m-2", "user-a", "tenant-a", "employee-a2", Role.MANAGER)
    memberships = FakeMembershipSource(
        memberships_by_credential={("tenant-a", "user-a"): [first, second]}
    )
    service = build_service(memberships)

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert error.value.code is IdentityErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_cross_tenant_credential_is_isolated() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-b", "user-b"): [
                membership("m-b", "user-b", "tenant-b", "employee-b1", Role.EMPLOYEE)
            ]
        }
    )
    service = build_service(memberships)

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-b"), AS_OF)

    assert error.value.code is IdentityErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_owner_gets_whole_tenant_scope_without_scope_source_call() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "owner"): [
                membership("m-owner", "owner", "tenant-a", "employee-o", Role.OWNER)
            ]
        }
    )
    scopes = FakeScopeSource()
    service = AuthorizationService(
        memberships=memberships,
        assignments=FakeOrganizationSource(),
        scopes=scopes,
        versions=SeededVersionAssigner(),
    )

    resolved = await service.authorize(credential("tenant-a", "owner"), AS_OF)

    assert resolved.data_scope.is_whole_tenant()
    assert scopes.calls == 0


@pytest.mark.asyncio
async def test_manager_scope_includes_self_and_managed_departments() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "mgr"): [
                membership(
                    "m-mgr", "mgr", "tenant-a", "employee-m9", Role.MANAGER, ("workshop-a1",)
                )
            ]
        }
    )
    scopes = FakeScopeSource(
        scopes_by_membership={
            "m-mgr": ((frozenset(), frozenset()),),
        }
    )
    assignments = FakeOrganizationSource(
        assignments_by_employee={"employee-m9": (("group-a1", "group-a2"),)}
    )
    service = AuthorizationService(
        memberships=memberships,
        assignments=assignments,
        scopes=scopes,
        versions=SeededVersionAssigner(),
    )

    resolved = await service.authorize(credential("tenant-a", "mgr"), AS_OF)

    assert resolved.data_scope.dept_ids is not None
    dept_ids = {str(item) for item in resolved.data_scope.dept_ids}
    assert dept_ids == {"workshop-a1", "group-a1", "group-a2"}
    assert resolved.data_scope.employee_ids is not None
    employee_ids = {str(item) for item in resolved.data_scope.employee_ids}
    assert "employee-m9" in employee_ids


@pytest.mark.asyncio
async def test_empty_scope_source_fails_closed() -> None:
    memberships = FakeMembershipSource(
        memberships_by_credential={
            ("tenant-a", "user-a"): [
                membership("m-a", "user-a", "tenant-a", "employee-a1", Role.EMPLOYEE)
            ]
        }
    )
    service = AuthorizationService(
        memberships=memberships,
        assignments=FakeOrganizationSource(),
        scopes=FakeScopeSource(scopes_by_membership={}),
        versions=SeededVersionAssigner(),
    )

    with pytest.raises(IdentityRejectionError) as error:
        await service.authorize(credential("tenant-a", "user-a"), AS_OF)

    assert error.value.code is IdentityErrorCode.INTERNAL_ERROR
