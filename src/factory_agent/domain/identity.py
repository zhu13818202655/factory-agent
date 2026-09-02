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
    """Display-only role tier (employee/manager/boss).

    Roles never enter authorization decisions; capability availability is
    decided by whether MES returns data plus the capability registry. The enum
    exists for presentation mapping; alignment with the four-role customer
    model (00/01/02/99) is tracked in Story #1.
    """

    EMPLOYEE = "employee"
    MANAGER = "manager"
    OWNER = "owner"


@dataclass(frozen=True, slots=True)
class TenantMembership:
    """Unique tenant membership derived from the credential bundle.

    ``tenant_id`` is the plaintext app_key and ``employee_id`` is the token
    ``user``; one factory has one AppKey, so membership is naturally unique.
    """

    user_id: UserId
    tenant_id: TenantId
    employee_id: EmployeeId
    display_name: str
    role: Role


@dataclass(frozen=True, slots=True)
class Identity:
    """Trusted identity: the credential pair behind one interaction.

    ``tenant_id`` is the plaintext app_key and ``user_id`` is the token
    ``user`` (work number); both come only from the credential bundle.
    """

    tenant_id: TenantId
    user_id: UserId


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
    """Minimal provable in-tenant data scope.

    Row-level filtering beyond the caller's own record is performed by the
    customer MES; ``mes_filtered=True`` records this trust and never claims the
    wider range itself. The flag can only be set at the adapter boundary — it
    has no broadening API and cannot be set by user input or model output.

    ``employee_ids`` currently contains only the caller's own work number;
    ``dept_ids`` comes from ``EmployeeQuery``/``DeptQuery`` current membership
    (latest relations only, no history).
    """

    tenant_id: TenantId
    employee_ids: frozenset[EmployeeId]
    dept_ids: frozenset[DeptId]
    evaluated_at: datetime
    scope_version: ScopeVersion
    mes_filtered: bool = False

    def is_whole_tenant(self) -> bool:
        """Deprecated whole-tenant probe; kept for audit compatibility.

        With MES-side filtering there is no locally proven whole-tenant scope;
        this returns False unless the caller explicitly recorded a platform-
        reviewed exception via ``mes_filtered``.
        """
        return False

    def narrow_to_employees(self, employee_ids: frozenset[EmployeeId]) -> DataScope | None:
        """Intersect employee IDs into the scope; empty intersection yields None."""
        narrowed = self.employee_ids & employee_ids
        if not narrowed:
            return None
        return replace(self, employee_ids=narrowed)

    def narrow_to_depts(self, dept_ids: frozenset[DeptId]) -> DataScope | None:
        """Intersect department IDs into the scope; empty intersection yields None."""
        narrowed = self.dept_ids & dept_ids
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
    "PlatformCapability",
    "PlatformScope",
    "PrincipalId",
    "Role",
    "ScopeVersion",
    "TenantContext",
    "TenantMembership",
    "UserId",
]
