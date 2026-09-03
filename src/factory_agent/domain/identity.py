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
    """Authoritative four-role tier returned by the customer token endpoint.

    The MES ``/api/system/token`` response ``roles`` field is the source of
    truth (00 员工 / 01 组长 / 02 管理 / 99 老板，见
    ``docs/product/需求及方案整理.md``「客户确认结论」与
    ``docs/product/AI问答对外接口-整理.md`` §2.1）。Role gates capability
    availability through the reviewed capability-role matrix
    (``application/permission_matrix.py``); business-data visibility itself is
    enforced by MES-side row filtering (``DataScope.mes_filtered``).
    """

    EMPLOYEE = "employee"
    GROUP_LEADER = "group_leader"
    MANAGER = "manager"
    OWNER = "owner"

    @classmethod
    def from_mes_code(cls, code: str) -> Role:
        """Map the customer ``roles`` code to the domain role.

        Unknown codes are rejected so an unreviewed role value can never enter
        authorization decisions.
        """
        try:
            return _ROLE_BY_MES_CODE[code.strip()]
        except KeyError as error:
            raise ValueError(f"unknown MES role code: {code!r}") from error

    @property
    def mes_code(self) -> str:
        return _MES_CODE_BY_ROLE[self]


_ROLE_BY_MES_CODE: dict[str, Role] = {
    "00": Role.EMPLOYEE,
    "01": Role.GROUP_LEADER,
    "02": Role.MANAGER,
    "99": Role.OWNER,
}
_MES_CODE_BY_ROLE: dict[Role, str] = {role: code for code, role in _ROLE_BY_MES_CODE.items()}


@dataclass(frozen=True, slots=True)
class TenantMembership:
    """Unique tenant membership derived from the credential bundle.

    ``tenant_id`` is the plaintext app_key and ``employee_id`` is the token
    ``user``; one factory has one AppKey, so membership is naturally unique.
    ``role`` and ``bound_dept_ids`` are authoritative token fields: the token
    response ``roles`` value plus the bound department/workshop set (managers
    may bind several departments across workshops; the binding is returned by
    the token endpoint at login — 客户确认，见
    ``docs/product/需求及方案整理.md``「客户确认结论」).
    """

    user_id: UserId
    tenant_id: TenantId
    employee_id: EmployeeId
    display_name: str
    role: Role
    bound_dept_ids: tuple[DeptId, ...] = ()


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
    """Active tenant binding for one MES business interaction.

    ``role`` is the authoritative token role (00/01/02/99 mapped); it gates
    capability availability and shapes user-facing range descriptions.
    """

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
    ``dept_ids`` comes from the token-returned binding (the caller's own
    department for employees, the bound group for group leaders, the bound
    department/workshop set for managers, and the whole-tenant stance for the
    boss role is expressed through MES-side filtering).
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
