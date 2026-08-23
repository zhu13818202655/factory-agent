"""Capability identifiers and the role-based authorization matrix.

Source of truth: docs/product/permission-matrix.md (FR-001~FR-012).
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
    GROUP_RANKING = "FR-004"
    ORDER_PROGRESS = "FR-005"
    ORDER_OUTPUT = "FR-006"
    ORG_COMPARISON = "FR-007"
    TEAM_PAYROLL_LIST = "FR-008"
    FACTORY_ORDER_OVERVIEW = "FR-009"
    WORKSHOP_OUTPUT_OVERVIEW = "FR-010"
    FACTORY_PAYROLL_STATS = "FR-011"
    ANY_EMPLOYEE_PAYROLL = "FR-012"


EMPLOYEE_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.OWN_OUTPUT,
        Capability.OWN_PAYROLL_SUMMARY,
        Capability.OWN_PAYROLL_DETAIL,
        Capability.GROUP_RANKING,
    }
)

MANAGER_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.OWN_OUTPUT,
        Capability.OWN_PAYROLL_SUMMARY,
        Capability.OWN_PAYROLL_DETAIL,
        Capability.ORDER_PROGRESS,
        Capability.ORDER_OUTPUT,
        Capability.ORG_COMPARISON,
        Capability.TEAM_PAYROLL_LIST,
    }
)

OWNER_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.FACTORY_ORDER_OVERVIEW,
        Capability.WORKSHOP_OUTPUT_OVERVIEW,
        Capability.FACTORY_PAYROLL_STATS,
        Capability.ANY_EMPLOYEE_PAYROLL,
    }
)

ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.EMPLOYEE: EMPLOYEE_CAPABILITIES,
    Role.MANAGER: MANAGER_CAPABILITIES,
    Role.OWNER: OWNER_CAPABILITIES,
}


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Structured allow/deny result for one capability request."""

    allowed: bool
    capability_id: str
    reason: str | None = None
    available_capabilities: tuple[str, ...] = ()

    @staticmethod
    def deny(capability_id: str, reason: str, role: Role) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=False,
            capability_id=capability_id,
            reason=reason,
            available_capabilities=tuple(sorted(item.value for item in ROLE_CAPABILITIES[role])),
        )


def authorize_capability(
    capability: Capability, context: TenantContext, scope: DataScope
) -> AuthorizationDecision:
    """Check the role-capability matrix for an already-resolved interaction."""
    if scope.tenant_id != context.tenant_id:
        return AuthorizationDecision.deny(
            capability.value,
            "data scope does not belong to the active tenant",
            context.role,
        )
    if capability in ROLE_CAPABILITIES[context.role]:
        return AuthorizationDecision(allowed=True, capability_id=capability.value)
    return AuthorizationDecision.deny(
        capability.value,
        "role is not permitted to use this capability",
        context.role,
    )
