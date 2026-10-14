from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    NarrowedFilters,
    TenantContext,
    TenantId,
)
from factory_agent.ports.contracts import MembershipResolver, TrustedCredential


@dataclass(frozen=True, slots=True)
class SourceAuthorization:
    tenant_context: TenantContext
    data_scope: DataScope


class VerificationOrganizations(Protocol):
    async def list_current_depts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> tuple[DeptId, ...]: ...


class VerificationPolicy(Protocol):
    async def authorize(
        self,
        credential: TrustedCredential,
        memberships: MembershipResolver,
        organizations: VerificationOrganizations,
        now: datetime,
    ) -> SourceAuthorization: ...

    def narrow_fact(
        self, authorization: SourceAuthorization, dataset_id: str
    ) -> NarrowedFilters | None: ...
