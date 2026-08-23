"""Use cases resolving trusted identity, tenant context, and data scopes."""

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
    Role,
    ScopeVersion,
    TenantContext,
    TenantId,
    TenantMembership,
)
from factory_agent.ports.contracts import TrustedCredential


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
    """Port over Canonical A1: resolve the unique membership for a credential."""

    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership: ...


class OrganizationSource(Protocol):
    """Port over Canonical A2: list assignments active in a time window."""

    async def list_assignments(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        start: datetime,
        end: datetime,
    ) -> tuple[tuple[DeptId, ...], ...]: ...


class ScopeSource(Protocol):
    """Port over Canonical A3: list effective scopes for the membership."""

    async def list_scopes(
        self, tenant_id: TenantId, membership_id: str, as_of: datetime
    ) -> tuple[tuple[frozenset[EmployeeId], frozenset[DeptId]], ...]: ...


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
    """Resolves trusted authorization before any business-data API call."""

    def __init__(
        self,
        memberships: MembershipSource,
        assignments: OrganizationSource,
        scopes: ScopeSource,
        versions: ScopeVersionAssigner,
    ) -> None:
        self._memberships = memberships
        self._assignments = assignments
        self._scopes = scopes
        self._versions = versions

    async def authorize(
        self, credential: TrustedCredential, as_of: datetime
    ) -> ResolvedAuthorization:
        membership = await self._resolve_membership(credential, as_of)
        identity = Identity(
            tenant_id=membership.tenant_id,
            user_id=membership.user_id,
            membership=membership,
        )
        tenant_context = TenantContext(
            tenant_id=membership.tenant_id,
            user_id=membership.user_id,
            employee_id=membership.employee_id,
            role=membership.role,
            resolved_at=as_of,
        )
        data_scope = await self._compute_scope(membership, as_of)
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
                "no active membership for the credential pair",
            ) from error
        except RuntimeError as error:
            raise IdentityRejectionError(
                IdentityErrorCode.INTERNAL_ERROR,
                "credential must resolve to exactly one membership",
            ) from error

        if membership.tenant_id != credential.tenant_id or membership.user_id != credential.user_id:
            raise IdentityRejectionError(
                IdentityErrorCode.FORBIDDEN,
                "resolved membership does not match the credential pair",
            )
        if not membership.is_active_at(as_of):
            raise IdentityRejectionError(
                IdentityErrorCode.NOT_FOUND,
                "membership is not active at the requested time",
            )
        return membership

    async def _compute_scope(self, membership: TenantMembership, as_of: datetime) -> DataScope:
        version = self._versions.new_version()
        if membership.role is Role.OWNER:
            return DataScope.whole_tenant(membership.tenant_id, as_of, version)

        scope_entries = await self._scopes.list_scopes(
            membership.tenant_id, str(membership.membership_id), as_of
        )
        if not scope_entries:
            raise IdentityRejectionError(
                IdentityErrorCode.INTERNAL_ERROR,
                "effective scope source returned no entries",
            )

        employee_ids: set[EmployeeId] = {membership.employee_id}
        dept_ids: set[DeptId] = set(membership.dept_ids)
        for entry_employees, entry_depts in scope_entries:
            employee_ids |= entry_employees
            dept_ids |= entry_depts

        if membership.role is Role.MANAGER:
            managed = await self._assignments.list_assignments(
                membership.tenant_id, membership.employee_id, membership.valid_from, as_of
            )
            for assignment_depts in managed:
                dept_ids |= set(assignment_depts)

        if not employee_ids or not dept_ids:
            raise IdentityRejectionError(
                IdentityErrorCode.INTERNAL_ERROR,
                "effective scope computation produced an empty scope",
            )
        return DataScope(
            tenant_id=membership.tenant_id,
            employee_ids=frozenset(employee_ids),
            dept_ids=frozenset(dept_ids),
            evaluated_at=as_of,
            scope_version=version,
        )


class FixedScopeVersionAssigner:
    """Deterministic version assigner for tests and offline composition."""

    def __init__(self, prefix: str = "scope") -> None:
        self._prefix = prefix
        self._counter = 0

    def new_version(self) -> ScopeVersion:
        self._counter += 1
        return ScopeVersion(f"{self._prefix}-{self._counter:08d}")
