"""Platform operation scope and RBAC.

usage-admin is a separate identity domain (ADR-0003): platform principals never
reuse factory MES roles, and their scope is a reviewed ``PlatformScope`` that
only narrows which tenants and which report capabilities they may touch. The
trusted gateway injects the principal via headers; request bodies never carry
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlatformRole(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"

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
        return self.role == PlatformRole.ANALYST

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


PRINCIPAL_HEADER = "X-Platform-Principal"
ROLE_HEADER = "X-Platform-Role"
TENANT_HEADER = "X-Platform-Tenants"


def resolve_platform_scope(
    principal: str | None,
    role_raw: str | None,
    tenants_raw: str | None,
) -> PlatformScope:
    """Derive a ``PlatformScope`` from trusted gateway headers."""
    if principal is None or not principal.strip():
        raise PlatformScopeError("platform principal is missing")
    role = PlatformRole.parse(role_raw)
    if role is None:
        raise PlatformScopeError("platform role is missing or unsupported")
    tenant_ids = _parse_tenants(tenants_raw)
    return PlatformScope(
        principal_id=principal.strip(),
        role=role,
        tenant_ids=tenant_ids,
    )


def _parse_tenants(raw: str | None) -> frozenset[str]:
    if raw is None or not raw.strip():
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


__all__ = [
    "PRINCIPAL_HEADER",
    "PlatformRole",
    "PlatformScope",
    "PlatformScopeError",
    "ROLE_HEADER",
    "TENANT_HEADER",
    "resolve_platform_scope",
]
