"""Use cases resolving trusted identity, tenant context, and data scopes.

Story 5 rework: the A1/A2/A3 membership-closure chain is replaced by the
customer credential bundle (M1/M4/M15). ``tenant_id`` is the plaintext
``app_key`` and ``employee_id`` is the token ``user``; one factory has one
AppKey, so membership is naturally unique (M4) — there is no multi-hit branch.
Roles are display-only (M11): capability availability is decided by whether
MES returns data plus the capability registry, never by a role matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    Identity,
    ScopeVersion,
    TenantContext,
    TenantId,
    TenantMembership,
)
from factory_agent.ports.contracts import TrustedCredential

__all__ = [
    "AuthorizationService",
    "FixedScopeVersionAssigner",
    "IdentityErrorCode",
    "IdentityRejectionError",
    "MembershipSource",
    "OrganizationSource",
    "ResolvedAuthorization",
    "ScopeVersionAssigner",
    "TenantMembership",
]


class IdentityErrorCode(StrEnum):
    """Bounded rejection codes for identity and scope resolution."""

    UNAUTHENTICATED = "unauthenticated"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    INTERNAL_ERROR = "internal_error"


class IdentityRejectionError(Exception):
    """Structured rejection raised before any business-data access."""

    def __init__(self, code: IdentityErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MembershipSource(Protocol):
    """Port resolving the unique membership behind a trusted credential.

    Implementations derive the binding from the customer credential bundle:
    tenant from plaintext app_key, employee from token ``user``. They raise
    ``LookupError`` when no active employee record exists.
    """

    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership: ...


class OrganizationSource(Protocol):
    """Port over EmployeeQuery/DeptQuery current department membership (K2)."""

    async def list_current_depts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> tuple[DeptId, ...]: ...


class ScopeVersionAssigner(Protocol):
    """Port assigning an opaque, unique version token per scope evaluation."""

    def new_version(self) -> ScopeVersion: ...


@dataclass(frozen=True, slots=True)
class ResolvedAuthorization:
    """Trusted identity, active tenant context, and immutable data scope."""

    identity: Identity
    tenant_context: TenantContext
    data_scope: DataScope


class AuthorizationService:
    """Resolves trusted authorization before any business-data API call.

    The computed scope is the minimal provable range: the caller's own
    employee ID plus their current departments. Wider visibility comes only
    from MES-side row filtering, recorded as ``mes_filtered`` at the adapter
    boundary — never claimed here.
    """

    def __init__(
        self,
        memberships: MembershipSource,
        organizations: OrganizationSource,
        versions: ScopeVersionAssigner,
    ) -> None:
        self._memberships = memberships
        self._organizations = organizations
        self._versions = versions

    async def authorize(
        self, credential: TrustedCredential, as_of: datetime
    ) -> ResolvedAuthorization:
        membership = await self._resolve_membership(credential, as_of)
        identity = Identity(
            tenant_id=membership.tenant_id,
            user_id=membership.user_id,
        )
        tenant_context = TenantContext(
            tenant_id=membership.tenant_id,
            user_id=membership.user_id,
            employee_id=membership.employee_id,
            role=membership.role,
            resolved_at=as_of,
        )
        dept_ids = await self._current_depts(membership.tenant_id, membership.employee_id)
        version = self._versions.new_version()
        data_scope = DataScope(
            tenant_id=membership.tenant_id,
            employee_ids=frozenset({membership.employee_id}),
            dept_ids=frozenset(dept_ids),
            evaluated_at=as_of,
            scope_version=version,
            mes_filtered=False,
        )
        return ResolvedAuthorization(
            identity=identity,
            tenant_context=tenant_context,
            data_scope=data_scope,
        )

    async def _resolve_membership(
        self, credential: TrustedCredential, as_of: datetime
    ) -> TenantMembership:
        try:
            membership = await self._memberships.resolve(credential, as_of)
        except LookupError as error:
            raise IdentityRejectionError(
                IdentityErrorCode.NOT_FOUND,
                "no active employee record for the credential",
            ) from error
        except RuntimeError as error:
            raise IdentityRejectionError(
                IdentityErrorCode.INTERNAL_ERROR,
                "credential resolution failed",
            ) from error

        if membership.tenant_id != credential.tenant_id or membership.user_id != credential.user_id:
            raise IdentityRejectionError(
                IdentityErrorCode.FORBIDDEN,
                "resolved membership does not match the credential",
            )
        return membership

    async def _current_depts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> tuple[DeptId, ...]:
        try:
            return await self._organizations.list_current_depts(tenant_id, employee_id)
        except LookupError as error:
            raise IdentityRejectionError(
                IdentityErrorCode.NOT_FOUND,
                "no current department membership for the employee",
            ) from error


class FixedScopeVersionAssigner:
    """Deterministic version assigner for tests and offline composition."""

    def __init__(self, prefix: str = "scope") -> None:
        self._prefix = prefix
        self._counter = 0

    def new_version(self) -> ScopeVersion:
        self._counter += 1
        return ScopeVersion(f"{self._prefix}-{self._counter:08d}")
