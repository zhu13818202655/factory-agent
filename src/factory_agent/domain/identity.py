"""Authorization domain values for trusted identity and tenant scoping."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Self

from factory_agent.domain.identifiers import TenantId


class NonEmptyId(str):
    """A non-empty identifier string with no surrounding whitespace."""

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not value or value != value.strip():
            raise ValueError("identifier must be non-empty and have no surrounding whitespace")
        return str.__new__(cls, value)


class UserId(NonEmptyId):
    """Identifier of a user account inside one deployment."""


class EmployeeId(NonEmptyId):
    """Identifier of an employee record inside one tenant."""


class DeptId(NonEmptyId):
    """Identifier of an organization node inside one tenant."""


class MembershipId(NonEmptyId):
    """Identifier of one authorized tenant membership."""


class ScopeVersion(NonEmptyId):
    """Opaque version token bound to one DataScope evaluation."""


class PrincipalId(NonEmptyId):
    """Identifier of a platform operations principal."""


class CapabilityId(NonEmptyId):
    """Stable identifier of one product capability (e.g. FR-001)."""


class Role(str, Enum):
    """Canonical single role attached to every tenant membership."""

    EMPLOYEE = "employee"
    MANAGER = "manager"
    OWNER = "owner"


@dataclass(frozen=True, slots=True)
class TenantMembership:
    """One authorized membership resolved from the trusted credential pair.

    Mirrors the Canonical ``TenantMembership`` schema: exactly one role per
    membership; manager scope semantics already include the member's own data.
    """

    membership_id: MembershipId
    user_id: UserId
    tenant_id: TenantId
    employee_id: EmployeeId
    role: Role
    dept_ids: tuple[DeptId, ...]
    valid_from: datetime
    valid_to: datetime | None

    def is_active_at(self, instant: datetime) -> bool:
        if self.valid_from > instant:
            return False
        return self.valid_to is None or instant < self.valid_to


@dataclass(frozen=True, slots=True)
class Identity:
    """Trusted identity: credential pair plus its unique authorized membership."""

    tenant_id: TenantId
    user_id: UserId
    membership: TenantMembership


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Active tenant binding for one MES business interaction."""

    tenant_id: TenantId
    user_id: UserId
    employee_id: EmployeeId
    role: Role
    resolved_at: datetime


class PlatformCapability(str, Enum):
    """Bounded platform operation capabilities, independent of factory roles."""

    USAGE_AGGREGATE = "usage_aggregate"
    USAGE_REPORT = "usage_report"


@dataclass(frozen=True, slots=True)
class PlatformScope:
    """Authorization for platform operations across listed tenants.

    Deliberately shares no inheritance or conversion path with ``DataScope``.
    """

    principal_id: PrincipalId
    tenant_ids: frozenset[TenantId]
    capabilities: frozenset[PlatformCapability] = field(default_factory=lambda: frozenset())

    def allows(self, capability: PlatformCapability) -> bool:
        return capability in self.capabilities

    def covers(self, tenant_id: TenantId) -> bool:
        return tenant_id in self.tenant_ids


class OwnerMarker:
    """Sentinel marking whole-tenant visibility for owner-role scopes."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "WholeTenant()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OwnerMarker)

    def __hash__(self) -> int:
        return hash(OwnerMarker)


WHOLE_TENANT = OwnerMarker()


@dataclass(frozen=True, slots=True)
class DataScope:
    """Immutable in-tenant data scope that can only be narrowed.

    ``employee_ids``/``dept_ids`` are ``None`` only when the scope covers the
    whole active tenant (owner role); otherwise they are explicit ID sets.
    """

    tenant_id: TenantId
    employee_ids: frozenset[EmployeeId] | None
    dept_ids: frozenset[DeptId] | None
    evaluated_at: datetime
    scope_version: ScopeVersion

    @staticmethod
    def whole_tenant(
        tenant_id: TenantId, evaluated_at: datetime, scope_version: ScopeVersion
    ) -> DataScope:
        return DataScope(
            tenant_id=tenant_id,
            employee_ids=None,
            dept_ids=None,
            evaluated_at=evaluated_at,
            scope_version=scope_version,
        )

    def is_whole_tenant(self) -> bool:
        return self.employee_ids is None and self.dept_ids is None

    def narrow_to_employees(self, employee_ids: frozenset[EmployeeId]) -> DataScope | None:
        """Intersect employee IDs into the scope; empty intersection yields None."""
        if self.is_whole_tenant():
            return replace(self, employee_ids=frozenset(employee_ids))
        current = self.employee_ids or frozenset()
        narrowed = current & employee_ids
        if not narrowed:
            return None
        return replace(self, employee_ids=narrowed)

    def narrow_to_depts(self, dept_ids: frozenset[DeptId]) -> DataScope | None:
        """Intersect department IDs into the scope; empty intersection yields None."""
        if self.is_whole_tenant():
            return replace(self, dept_ids=frozenset(dept_ids))
        current = self.dept_ids or frozenset()
        narrowed = current & dept_ids
        if not narrowed:
            return None
        return replace(self, dept_ids=narrowed)


__all__ = [
    "CapabilityId",
    "DataScope",
    "DeptId",
    "EmployeeId",
    "Identity",
    "MembershipId",
    "NonEmptyId",
    "OwnerMarker",
    "PlatformCapability",
    "PlatformScope",
    "PrincipalId",
    "Role",
    "ScopeVersion",
    "TenantContext",
    "TenantMembership",
    "UserId",
    "WHOLE_TENANT",
]
