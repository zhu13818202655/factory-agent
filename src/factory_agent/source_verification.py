from datetime import datetime

from factory_agent.application.authorization import AuthorizationService, FixedScopeVersionAssigner
from factory_agent.application.filters import FilterNarrower
from factory_agent.application.permission_matrix import Capability, authorize_capability
from factory_agent.data_api.verify_analysis_sources import main as verification_main
from factory_agent.domain import NarrowedFilters, Role
from factory_agent.observability.context import bind_tenant_id, current_tenant_id
from factory_agent.ports.contracts import MembershipResolver, TrustedCredential
from factory_agent.ports.source_verification import SourceAuthorization, VerificationOrganizations


class FactoryVerificationPolicy:
    async def authorize(
        self,
        credential: TrustedCredential,
        memberships: MembershipResolver,
        organizations: VerificationOrganizations,
        now: datetime,
    ) -> SourceAuthorization:
        previous_tenant = current_tenant_id()
        try:
            authorized = await AuthorizationService(
                memberships, organizations, FixedScopeVersionAssigner("verification")
            ).authorize(credential, now)
        finally:
            bind_tenant_id(previous_tenant)
        return SourceAuthorization(authorized.tenant_context, authorized.data_scope)

    def narrow_fact(
        self, authorization: SourceAuthorization, dataset_id: str
    ) -> NarrowedFilters | None:
        if dataset_id == "production_progress":
            capability = Capability.ORDER_PROGRESS
        elif dataset_id == "reported_production":
            capability = (
                Capability.OWN_OUTPUT
                if authorization.tenant_context.role is Role.EMPLOYEE
                else Capability.ORDER_OUTPUT
            )
        else:
            return None
        if not authorize_capability(
            capability, authorization.tenant_context, authorization.data_scope
        ).allowed:
            return None
        return FilterNarrower().narrow(
            authorization.data_scope,
            restrict_to_scope_employees=authorization.tenant_context.role is Role.EMPLOYEE,
        )


def main() -> int:
    return verification_main(FactoryVerificationPolicy())


if __name__ == "__main__":
    raise SystemExit(main())
