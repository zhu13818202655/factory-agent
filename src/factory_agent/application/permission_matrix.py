"""Capability identifiers and the authoritative capability-role matrix.

Source of truth: the function tables in
``docs/product/需求及方案整理.md``（员工/管理/老板 功能表 + 「客户确认结论」）.
The MES ``/api/system/token`` ``roles`` code is the authoritative role; the
matrix decides which capabilities each role may use. Business-data visibility
inside an allowed capability is still enforced by MES-side row filtering
(``DataScope.mes_filtered``); the agent never re-filters rows.

Matrix (customer-confirmed):
- FR-001..FR-004 personal capabilities: all four roles (self dimension).
- FR-005..FR-008 management capabilities: 01 组长 / 02 管理.
- FR-009..FR-012 factory-wide capabilities: 99 老板 only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from factory_agent.domain import DataScope, Role, TenantContext


class Capability(StrEnum):
    """Product capability identifiers from the permission matrix."""

    OWN_OUTPUT = "FR-001"
    OWN_PAYROLL_SUMMARY = "FR-002"
    OWN_PAYROLL_DETAIL = "FR-003"
    GROUP_INCOME_RANK = "FR-004"
    ORDER_PROGRESS = "FR-005"
    ORDER_OUTPUT = "FR-006"
    WORKSHOP_COMPARISON = "FR-007"
    TEAM_PAYROLL_LIST = "FR-008"
    FACTORY_ORDER_OVERVIEW = "FR-009"
    WORKSHOP_OUTPUT_OVERVIEW = "FR-010"
    FACTORY_PAYROLL_STATS = "FR-011"
    ANY_EMPLOYEE_PAYROLL = "FR-012"


#: All four roles (personal capabilities operate on the caller's own record).
_ALL_ROLES: frozenset[Role] = frozenset(
    {Role.EMPLOYEE, Role.GROUP_LEADER, Role.MANAGER, Role.OWNER}
)
#: Management capabilities: group leaders and managers (customer function
#: tables「管理」apply to 01/02; the boss role uses the factory-wide set).
_MANAGEMENT_ROLES: frozenset[Role] = frozenset({Role.GROUP_LEADER, Role.MANAGER})
#: Factory-wide capabilities: owner only.
_OWNER_ROLES: frozenset[Role] = frozenset({Role.OWNER})

#: Reviewed capability-role matrix; convergence point for Story 2's expected
#: range definitions as well.
CAPABILITY_ROLES: dict[Capability, frozenset[Role]] = {
    Capability.OWN_OUTPUT: _ALL_ROLES,
    Capability.OWN_PAYROLL_SUMMARY: _ALL_ROLES,
    Capability.OWN_PAYROLL_DETAIL: _ALL_ROLES,
    Capability.GROUP_INCOME_RANK: _ALL_ROLES,
    Capability.ORDER_PROGRESS: _MANAGEMENT_ROLES,
    Capability.ORDER_OUTPUT: _MANAGEMENT_ROLES,
    Capability.WORKSHOP_COMPARISON: _MANAGEMENT_ROLES,
    Capability.TEAM_PAYROLL_LIST: _MANAGEMENT_ROLES,
    Capability.FACTORY_ORDER_OVERVIEW: _OWNER_ROLES,
    Capability.WORKSHOP_OUTPUT_OVERVIEW: _OWNER_ROLES,
    Capability.FACTORY_PAYROLL_STATS: _OWNER_ROLES,
    Capability.ANY_EMPLOYEE_PAYROLL: _OWNER_ROLES,
}

REGISTERED_CAPABILITIES: frozenset[Capability] = frozenset(CAPABILITY_ROLES)

#: User-facing data range per role (友好拒绝文案与快捷问题共用).
ROLE_DATA_RANGE: dict[Role, str] = {
    Role.EMPLOYEE: "本人的产量与工资数据",
    Role.GROUP_LEADER: "本人数据及所绑定小组的生产与工资数据",
    Role.MANAGER: "本人数据及所绑定车间/部门的生产与工资数据",
    Role.OWNER: "本人数据及全厂的订单、产量与工资数据",
}


def capabilities_for_role(role: Role) -> frozenset[Capability]:
    """Registered capabilities available to one role."""
    return frozenset(capability for capability, roles in CAPABILITY_ROLES.items() if role in roles)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Structured allow/deny result for one capability request."""

    allowed: bool
    capability_id: str
    reason: str | None = None
    available_capabilities: tuple[str, ...] = ()

    @staticmethod
    def deny(capability_id: str, reason: str, role: Role | None = None) -> AuthorizationDecision:
        if role is not None:
            available = tuple(sorted(item.value for item in capabilities_for_role(role)))
        else:
            available = tuple(sorted(item.value for item in REGISTERED_CAPABILITIES))
        return AuthorizationDecision(
            allowed=False,
            capability_id=capability_id,
            reason=reason,
            available_capabilities=available,
        )


def authorize_capability(
    capability: Capability, context: TenantContext, scope: DataScope
) -> AuthorizationDecision:
    """Check tenant binding, registry membership, and the role matrix.

    The role is authoritative: a capability outside the caller's matrix is
    denied before any business-data call.
    """
    if scope.tenant_id != context.tenant_id:
        return AuthorizationDecision.deny(
            capability.value,
            "data scope does not belong to the active tenant",
            context.role,
        )
    if capability not in REGISTERED_CAPABILITIES:
        return AuthorizationDecision.deny(
            capability.value,
            "capability is not registered",
            context.role,
        )
    if context.role not in CAPABILITY_ROLES[capability]:
        return AuthorizationDecision.deny(
            capability.value,
            "capability is not available for the current role",
            context.role,
        )
    return AuthorizationDecision(allowed=True, capability_id=capability.value)
