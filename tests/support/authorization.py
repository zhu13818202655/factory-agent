"""Fake sources for authorization use-case tests; deterministic and offline.

Story 5 rework mirrors the new ``AuthorizationService``: membership is unique
per credential (one factory, one AppKey — M4), the credential bundle supplies
``tenant_id``/``employee_id``, and department membership comes from
``EmployeeQuery``/``DeptQuery`` current relations (K2). Roles are display-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from factory_agent.application.authorization import (
    MembershipSource,
    OrganizationSource,
)
from factory_agent.domain import (
    DeptId,
    EmployeeId,
    Role,
    ScopeVersion,
    TenantId,
    TenantMembership,
    UserId,
)
from factory_agent.ports.contracts import TrustedCredential


def membership(
    user_id: str,
    tenant_id: str,
    employee_id: str,
    role: Role,
    display_name: str = "模拟员工",
) -> TenantMembership:
    """Build the unique customer membership for one credential."""
    return TenantMembership(
        user_id=UserId(user_id),
        tenant_id=TenantId(tenant_id),
        employee_id=EmployeeId(employee_id),
        display_name=display_name,
        role=role,
    )


@dataclass
class FakeMembershipSource(MembershipSource):
    """Resolves the single membership behind a trusted credential (M4).

    One factory has one AppKey, so each ``(tenant_id, user_id)`` maps to at
    most one membership; there is no ambiguity branch.
    """

    memberships_by_credential: dict[tuple[str, str], TenantMembership] = field(
        default_factory=lambda: {}
    )
    calls: int = 0

    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership:
        self.calls += 1
        key = (str(credential.tenant_id), str(credential.user_id))
        try:
            return self.memberships_by_credential[key]
        except KeyError as error:
            raise LookupError("no active employee record for the credential") from error


@dataclass
class FakeOrganizationSource(OrganizationSource):
    """``list_current_depts`` backed by a static employee → depts mapping."""

    depts_by_employee: dict[str, tuple[str, ...]] = field(default_factory=lambda: {})
    calls: int = 0

    async def list_current_depts(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
    ) -> tuple[DeptId, ...]:
        self.calls += 1
        depts = self.depts_by_employee.get(str(employee_id))
        if depts is None:
            raise LookupError("no current department membership for the employee")
        return tuple(DeptId(value) for value in depts)


@dataclass
class SeededVersionAssigner:
    prefix: str = "scope"
    counter: int = 0

    def new_version(self) -> ScopeVersion:
        self.counter += 1
        return ScopeVersion(f"{self.prefix}-{self.counter:08d}")


def credential(tenant_id: str, user_id: str) -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId(tenant_id), user_id=UserId(user_id))
