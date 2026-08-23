"""Framework-independent domain values and invariants."""

from factory_agent.domain.identifiers import InteractionId, TenantId
from factory_agent.domain.identity import (
    CapabilityId,
    DataScope,
    DeptId,
    EmployeeId,
    Identity,
    MembershipId,
    NonEmptyId,
    PlatformCapability,
    PlatformScope,
    PrincipalId,
    Role,
    ScopeVersion,
    TenantContext,
    TenantMembership,
    UserId,
)

__all__ = [
    "CapabilityId",
    "DataScope",
    "DeptId",
    "EmployeeId",
    "Identity",
    "InteractionId",
    "MembershipId",
    "NonEmptyId",
    "PlatformCapability",
    "PlatformScope",
    "PrincipalId",
    "Role",
    "ScopeVersion",
    "TenantContext",
    "TenantId",
    "TenantMembership",
    "UserId",
]
