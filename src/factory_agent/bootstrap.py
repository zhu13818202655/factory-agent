from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.capabilities import CapabilityRegistry
from factory_agent.application.intent import CapabilityCatalog, CapabilityIntentParser
from factory_agent.application.session import SessionLimits, SessionService
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.canonical import CanonicalMesAdapter
from factory_agent.domain import DeptId, EmployeeId, MesError, TenantId, TenantMembership
from factory_agent.llm.registry import ModelRegistry, load_model_registry
from factory_agent.llm.router_gateway import LiteLlmRouterGateway
from factory_agent.observability.audit import AuditSink, InMemoryAuditSink
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.session_store import SqlInteractionStore
from factory_agent.ports import (
    ArtifactStore,
    CapabilityRunner,
    Clock,
    IdentityProvider,
    InteractionStore,
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
    interactions: InteractionStore | None = None
    capability_runner: CapabilityRunner | None = None
    capability_catalog: CapabilityCatalog | None = None
    new_id: Callable[[], str] | None = None


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
    interactions: InteractionStore | None = None
    sessions_service: SessionService | None = None
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

    if supplied.model is not None:
        model: ModelGateway = supplied.model
        model_status = "fake"
    else:
        registry = _load_registry(settings)
        if registry is not None and registry.is_usable():
            model = LiteLlmRouterGateway(
                registry,
                default_timeout_seconds=settings.llm_timeout_seconds,
                default_temperature=settings.llm_temperature,
                default_top_p=settings.llm_top_p,
                default_max_output_tokens=settings.llm_max_output_tokens,
                num_retries=settings.llm_num_retries,
                allowed_fails=settings.llm_allowed_fails,
                cooldown_seconds=settings.llm_cooldown_seconds,
            )
            model_status = "configured"
        else:
            model = NotConfiguredModelGateway()
            model_status = "not_configured"

    if supplied.interactions is not None:
        interactions: InteractionStore | None = supplied.interactions
        interactions_status = "fake"
    elif settings.postgres_url is not None:
        interactions = SqlInteractionStore(create_session_engine(str(settings.postgres_url)))
        interactions_status = "configured"
    else:
        interactions = None
        interactions_status = "not_configured"

    readiness = {
        "identity": "fake" if supplied.identity is not None else "not_configured",
        "mes": mes_status,
        "model": model_status,
        "sessions": "fake" if supplied.sessions is not None else "not_configured",
        "artifacts": "fake" if supplied.artifacts is not None else "not_configured",
        "interactions": interactions_status,
        "postgres": "configured" if settings.postgres_url is not None else "not_configured",
        "litellm": model_status,
        "redis": "configured" if settings.redis_url is not None else "not_configured",
    }
    authorization = supplied.authorization or AuthorizationService(
        memberships=_UnresolvedMemberships(),
        assignments=_UnresolvedAssignments(),
        scopes=_UnresolvedScopes(),
        versions=FixedScopeVersionAssigner(),
    )
    clock = supplied.clock or SystemClock()
    return ApplicationContainer(
        settings=settings,
        capabilities=CapabilityRegistry(),
        identity=supplied.identity or NotConfiguredIdentityProvider(),
        mes=mes,
        model=model,
        sessions=supplied.sessions or NotConfiguredSessionRepository(),
        artifacts=supplied.artifacts or NotConfiguredArtifactStore(),
        clock=clock,
        authorization=authorization,
        audit=supplied.audit or InMemoryAuditSink(),
        interactions=interactions,
        sessions_service=_build_session_service(
            settings, supplied, interactions, authorization, clock, model
        ),
        readiness=readiness,
    )


def _build_session_service(
    settings: FactoryAgentSettings,
    supplied: DependencyOverrides,
    interactions: InteractionStore | None,
    authorization: AuthorizationService,
    clock: Clock,
    model: ModelGateway,
) -> SessionService | None:
    """Only compose the session pipeline when its dependencies exist."""
    if interactions is None or supplied.capability_runner is None:
        return None
    parser = CapabilityIntentParser(
        model,
        supplied.capability_catalog or CapabilityCatalog(),
        model_alias=settings.llm_fast_alias,
        timezone_name=settings.factory_timezone,
        max_repair_attempts=settings.llm_max_repair_attempts,
        max_history_turns=settings.session_history_max_turns,
        max_history_chars=settings.session_history_max_chars,
    )
    return SessionService(
        interactions,
        authorization,
        parser,
        supplied.capability_runner,
        clock,
        new_id=supplied.new_id or (lambda: uuid4().hex),
        limits=SessionLimits(
            max_input_chars=settings.session_max_input_chars,
            max_clarification_rounds=settings.session_max_clarification_rounds,
            heartbeat_seconds=settings.session_heartbeat_seconds,
        ),
    )


def _load_registry(settings: FactoryAgentSettings) -> ModelRegistry | None:
    """A missing or invalid registry degrades readiness instead of crashing startup."""
    try:
        return load_model_registry(settings.model_registry_path)
    except MesError:
        return None


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
