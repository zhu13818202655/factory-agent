"""Capability identifiers and the authorization matrix.

Story 5 rework (M11/A.1): the three-tier role matrix is display-only. Roles
never enter ``authorize()``; capability availability is decided by whether MES
returns data plus this capability registry. ``authorize_capability`` therefore
only verifies tenant binding — every registered capability is available to
every authenticated caller, and actual data visibility is enforced by MES-side
row filtering recorded in ``DataScope.mes_filtered``.

Source of truth: docs/product/permission-matrix.md (FR-001~FR-012, FR-004
cancelled per M7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from factory_agent.domain import DataScope, TenantContext


class Capability(StrEnum):
    """Product capability identifiers from the permission matrix."""

    OWN_OUTPUT = "FR-001"
    OWN_PAYROLL_SUMMARY = "FR-002"
    OWN_PAYROLL_DETAIL = "FR-003"
    # FR-004 (group income ranking) cancelled per M7: employee endpoints only
    # return the caller's own data.
    ORDER_PROGRESS = "FR-005"
    ORDER_OUTPUT = "FR-006"
    WORKSHOP_COMPARISON = "FR-007"
    TEAM_PAYROLL_LIST = "FR-008"
    FACTORY_ORDER_OVERVIEW = "FR-009"
    WORKSHOP_OUTPUT_OVERVIEW = "FR-010"
    FACTORY_PAYROLL_STATS = "FR-011"
    ANY_EMPLOYEE_PAYROLL = "FR-012"


REGISTERED_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.OWN_OUTPUT,
        Capability.OWN_PAYROLL_SUMMARY,
        Capability.OWN_PAYROLL_DETAIL,
        Capability.ORDER_PROGRESS,
        Capability.ORDER_OUTPUT,
        Capability.WORKSHOP_COMPARISON,
        Capability.TEAM_PAYROLL_LIST,
        Capability.FACTORY_ORDER_OVERVIEW,
        Capability.WORKSHOP_OUTPUT_OVERVIEW,
        Capability.FACTORY_PAYROLL_STATS,
        Capability.ANY_EMPLOYEE_PAYROLL,
    }
)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Structured allow/deny result for one capability request."""

    allowed: bool
    capability_id: str
    reason: str | None = None
    available_capabilities: tuple[str, ...] = ()

    @staticmethod
    def deny(capability_id: str, reason: str) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=False,
            capability_id=capability_id,
            reason=reason,
            available_capabilities=tuple(sorted(item.value for item in REGISTERED_CAPABILITIES)),
        )


def authorize_capability(
    capability: Capability, context: TenantContext, scope: DataScope
) -> AuthorizationDecision:
    """Check tenant binding and registry membership for a resolved interaction.

    Roles are display-only (M11): they never gate capabilities here.
    """
    if scope.tenant_id != context.tenant_id:
        return AuthorizationDecision.deny(
            capability.value,
            "data scope does not belong to the active tenant",
        )
    if capability not in REGISTERED_CAPABILITIES:
        return AuthorizationDecision.deny(
            capability.value,
            "capability is not registered",
        )
    return AuthorizationDecision(allowed=True, capability_id=capability.value)
