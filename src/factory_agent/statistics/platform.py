"""Platform operation scope and RBAC.

The statistics surface is a separate identity domain (ADR-0003): platform
principals never reuse factory MES roles, and their scope is a reviewed
``PlatformScope`` that only narrows which tenants and which report capabilities
they may touch. Identity is derived exclusively from a validated ``Bearer``
token; no request body and no unverified header may carry it.
"""

from dataclasses import dataclass
from enum import Enum


class PlatformRole(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"

    @classmethod
    def parse(cls, raw: str | None) -> "PlatformRole | None":
        if raw is None:
            return None
        try:
            return cls(raw)
        except ValueError:
            return None


class PlatformScopeError(Exception):
    """Raised when a platform request is missing or outside its scope."""


@dataclass(frozen=True, slots=True)
class PlatformScope:
    """Reviewed platform authorization; empty tenant set means platform-wide."""

    principal_id: str
    role: PlatformRole
    tenant_ids: frozenset[str]

    def allows_export(self) -> bool:
        return self.role in (PlatformRole.ANALYST, PlatformRole.ADMIN)

    def allows_manage_tenants(self) -> bool:
        """Factory-account CRUD is admin-only (D14)."""
        return self.role == PlatformRole.ADMIN

    def covers_tenant(self, tenant_id: str) -> bool:
        return not self.tenant_ids or tenant_id in self.tenant_ids

    def effective_tenants(self, requested: frozenset[str] | None) -> frozenset[str]:
        """Intersect a requested tenant filter with this principal's scope."""
        if not self.tenant_ids:
            return frozenset(requested or ())
        if requested is None:
            return self.tenant_ids
        return self.tenant_ids & requested

    def require_covers(self, requested: frozenset[str] | None) -> frozenset[str]:
        covered = self.effective_tenants(requested)
        if requested is not None and covered != requested:
            raise PlatformScopeError("requested tenant set exceeds the platform scope")
        return covered


__all__ = [
    "PlatformRole",
    "PlatformScope",
    "PlatformScopeError",
]
