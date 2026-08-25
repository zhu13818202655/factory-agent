"""Fake sources for authorization use-case tests; deterministic and offline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from factory_agent.application.authorization import (
    MembershipSource,
    OrganizationSource,
    ScopeSource,
)
from factory_agent.domain import (
    DeptId,
    EmployeeId,
    MembershipId,
    Role,
    ScopeVersion,
    TenantId,
    TenantMembership,
    UserId,
)
from factory_agent.ports.contracts import TrustedCredential
from tests.support.ports import FakeClock


def membership(
    membership_id: str,
    user_id: str,
    tenant_id: str,
    employee_id: str,
    role: Role,
    dept_ids: tuple[str, ...] = (),
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> TenantMembership:
    return TenantMembership(
        membership_id=MembershipId(membership_id),
        user_id=UserId(user_id),
        tenant_id=TenantId(tenant_id),
        employee_id=EmployeeId(employee_id),
        role=role,
        dept_ids=tuple(DeptId(value) for value in dept_ids),
        valid_from=valid_from or FakeClock().current,
        valid_to=valid_to,
    )


@dataclass
class FakeMembershipSource(MembershipSource):
    memberships_by_credential: dict[tuple[str, str], list[TenantMembership]] = field(
        default_factory=lambda: {}
    )
    calls: int = 0

    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership:
        self.calls += 1
        key = (str(credential.tenant_id), str(credential.user_id))
        candidates = [
            item for item in self.memberships_by_credential.get(key, []) if item.is_active_at(as_of)
        ]
        if not candidates:
            raise LookupError("no active membership")
        if len(candidates) > 1:
            raise RuntimeError("ambiguous membership")
        return candidates[0]


@dataclass
class FakeOrganizationSource(OrganizationSource):
    assignments_by_employee: dict[str, tuple[tuple[str, ...], ...]] = field(
        default_factory=lambda: {}
    )
    calls: int = 0

    async def list_assignments(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        start: datetime,
        end: datetime,
    ) -> tuple[tuple[DeptId, ...], ...]:
        self.calls += 1
        return tuple(
            tuple(DeptId(value) for value in depts)
            for depts in self.assignments_by_employee.get(str(employee_id), ())
        )


@dataclass
class FakeScopeSource(ScopeSource):
    scopes_by_membership: dict[str, tuple[tuple[frozenset[EmployeeId], frozenset[DeptId]], ...]] = (
        field(default_factory=lambda: {})
    )
    calls: int = 0

    async def list_scopes(
        self, tenant_id: TenantId, membership_id: str, as_of: datetime
    ) -> tuple[tuple[frozenset[EmployeeId], frozenset[DeptId]], ...]:
        self.calls += 1
        return self.scopes_by_membership.get(membership_id, ())


@dataclass
class SeededVersionAssigner:
    prefix: str = "scope"
    counter: int = 0

    def new_version(self) -> ScopeVersion:
        self.counter += 1
        return ScopeVersion(f"{self.prefix}-{self.counter:08d}")


def credential(tenant_id: str, user_id: str) -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId(tenant_id), user_id=UserId(user_id))
