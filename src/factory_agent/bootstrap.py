from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.capabilities import CapabilityRegistry
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.canonical import CanonicalMesAdapter
from factory_agent.domain import DeptId, EmployeeId, TenantId, TenantMembership
from factory_agent.observability.audit import AuditSink, InMemoryAuditSink
from factory_agent.ports import (
    ArtifactStore,
    Clock,
    IdentityProvider,
    MesDataSource,
    ModelGateway,
    SessionRepository,
    TrustedCredential,
)
from factory_agent.ports.not_configured import (
    DependencyNotConfiguredError,
    NotConfiguredArtifactStore,
    NotConfiguredIdentityProvider,
    NotConfiguredMesDataSource,
    NotConfiguredModelGateway,
    NotConfiguredSessionRepository,
)


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DependencyOverrides:
    identity: IdentityProvider | None = None
    mes: MesDataSource[Any, Any] | None = None
    model: ModelGateway | None = None
    sessions: SessionRepository | None = None
    artifacts: ArtifactStore | None = None
    clock: Clock | None = None
    authorization: AuthorizationService | None = None
    audit: AuditSink | None = None


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    settings: FactoryAgentSettings
    capabilities: CapabilityRegistry
    identity: IdentityProvider
    mes: MesDataSource[Any, Any]
    model: ModelGateway
    sessions: SessionRepository
    artifacts: ArtifactStore
    clock: Clock
    authorization: AuthorizationService
    audit: AuditSink
    readiness: dict[str, str] = field(default_factory=lambda: {})


def build_container(
    settings: FactoryAgentSettings, overrides: DependencyOverrides | None = None
) -> ApplicationContainer:
    supplied = overrides or DependencyOverrides()
    if supplied.mes is not None:
        mes = supplied.mes
        mes_status = "fake"
    elif settings.canonical_mes_base_url is not None:
        mes = CanonicalMesAdapter(str(settings.canonical_mes_base_url), "unconfigured")
        mes_status = "configured"
    else:
        mes = NotConfiguredMesDataSource()
        mes_status = "not_configured"

    readiness = {
        "identity": "fake" if supplied.identity is not None else "not_configured",
        "mes": mes_status,
        "model": "fake" if supplied.model is not None else "not_configured",
        "sessions": "fake" if supplied.sessions is not None else "not_configured",
        "artifacts": "fake" if supplied.artifacts is not None else "not_configured",
        "postgres": "configured" if settings.postgres_url is not None else "not_configured",
        "litellm": "configured" if settings.litellm_base_url is not None else "not_configured",
        "redis": "configured" if settings.redis_url is not None else "not_configured",
    }
    return ApplicationContainer(
        settings=settings,
        capabilities=CapabilityRegistry(),
        identity=supplied.identity or NotConfiguredIdentityProvider(),
        mes=mes,
        model=supplied.model or NotConfiguredModelGateway(),
        sessions=supplied.sessions or NotConfiguredSessionRepository(),
        artifacts=supplied.artifacts or NotConfiguredArtifactStore(),
        clock=supplied.clock or SystemClock(),
        authorization=supplied.authorization
        or AuthorizationService(
            memberships=_UnresolvedMemberships(),
            assignments=_UnresolvedAssignments(),
            scopes=_UnresolvedScopes(),
            versions=FixedScopeVersionAssigner(),
        ),
        audit=supplied.audit or InMemoryAuditSink(),
        readiness=readiness,
    )


class _UnresolvedMemberships:
    """Placeholder until the Canonical A1 adapter is wired in Story 3."""

    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership:
        raise DependencyNotConfiguredError("membership resolver is not configured")


class _UnresolvedAssignments:
    async def list_assignments(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        start: datetime,
        end: datetime,
    ) -> tuple[tuple[DeptId, ...], ...]:
        raise DependencyNotConfiguredError("organization source is not configured")


class _UnresolvedScopes:
    async def list_scopes(
        self, tenant_id: TenantId, membership_id: str, as_of: datetime
    ) -> tuple[tuple[frozenset[EmployeeId], frozenset[DeptId]], ...]:
        raise DependencyNotConfiguredError("scope source is not configured")
