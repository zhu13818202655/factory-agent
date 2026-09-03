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
from factory_agent.application.business_filters import BusinessFilterResolver
from factory_agent.application.cache import AuthAwareCache, CachedDirectorySource
from factory_agent.application.capabilities import CapabilityRegistry
from factory_agent.application.capability_map import default_capability_catalog
from factory_agent.application.consistency import ConsistencyValidator
from factory_agent.application.filters import FilterNarrower
from factory_agent.application.intent import CapabilityCatalog, CapabilityIntentParser
from factory_agent.application.personal import PersonalizationService
from factory_agent.application.session import SessionLimits, SessionService
from factory_agent.application.usage import ContextVarMesCallRecorder
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.directory import MesDirectorySource
from factory_agent.data_api.hongzhao import AdapterSettings, HongzhaoMesAdapter
from factory_agent.data_api.schemas import BASE_DATA_RESOURCES, ROW_MODEL_BY_RESOURCE
from factory_agent.data_api.token_gateway import (
    GatewayTokenRefresher,
    TokenBackedMembershipResolver,
    TokenCredentialExchange,
)
from factory_agent.domain import DeptId, EmployeeId, MesError, TenantId, UserId
from factory_agent.execution.executor import ScopedExecutor
from factory_agent.execution.kernel import KernelCapabilityRunner, KernelSettings
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.export_service import ExportService
from factory_agent.infrastructure.cache import RedisCacheStore
from factory_agent.llm.registry import ModelRegistry, load_model_registry
from factory_agent.llm.router_gateway import LiteLlmRouterGateway
from factory_agent.observability.audit import AuditSink, InMemoryAuditSink
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.persistence.personal_store import (
    SqlFavoriteRepository,
    SqlHistoryRepository,
    SqlUserMappingRepository,
)
from factory_agent.persistence.scope_violation import SqlScopeViolationStore
from factory_agent.persistence.session_store import SqlInteractionStore
from factory_agent.persistence.tenant_registry import SqlTenantRegistryReader
from factory_agent.ports import (
    ArtifactStore,
    CapabilityRunner,
    Clock,
    IdentityProvider,
    InteractionStore,
    MesCallRecorder,
    MesDataSource,
    ModelGateway,
    SessionRepository,
    TrustedCredential,
)
from factory_agent.ports.artifacts import ArtifactExporter
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
    artifact_exporter: ArtifactExporter | None = None
    capability_catalog: CapabilityCatalog | None = None
    personalization: PersonalizationService | None = None
    new_id: Callable[[], str] | None = None
    mes_call_recorder: MesCallRecorder | None = None
    credential_exchange: TokenCredentialExchange | None = None


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
    capability_runner: CapabilityRunner | None = None
    artifact_exporter: ArtifactExporter | None = None
    personalization: PersonalizationService | None = None
    cache: AuthAwareCache | None = None
    credential_exchange: TokenCredentialExchange | None = None
    readiness: dict[str, str] = field(default_factory=lambda: {})


def build_container(
    settings: FactoryAgentSettings, overrides: DependencyOverrides | None = None
) -> ApplicationContainer:
    supplied = overrides or DependencyOverrides()
    # One shared recorder feeds the MES adapter and the metering context; the
    # session pipeline drains it at each commit.
    mes_recorder: MesCallRecorder = supplied.mes_call_recorder or ContextVarMesCallRecorder()
    # The token gateway exchanges the caller's encrypted app_key at
    # /api/system/token and owns the live bundles. It exists exactly when a
    # canonical MES base URL is configured (single adapter, no second impl).
    credential_exchange: TokenCredentialExchange | None = supplied.credential_exchange
    if supplied.mes is not None:
        mes = supplied.mes
        mes_status = "fake"
    elif settings.canonical_mes_base_url is not None:
        tenant_registry = (
            SqlTenantRegistryReader(create_session_engine(str(settings.postgres_url)))
            if settings.postgres_url is not None
            else None
        )
        if credential_exchange is None:
            credential_exchange = TokenCredentialExchange(
                str(settings.canonical_mes_base_url),
                refresh_threshold_seconds=settings.mes_token_refresh_threshold_seconds,
            )
        mes = HongzhaoMesAdapter(
            str(settings.canonical_mes_base_url),
            # Placeholder bundle for readiness; the live per-caller bundle is
            # bound by the token gateway at the API boundary, never here.
            MesCredentialBundle(  # nosec B106 - placeholder, no real secret
                access_token="unconfigured",
                app_key="unconfigured",
                sign="unconfigured",
                timestamp=0,
                expires_at=datetime.max.replace(tzinfo=timezone.utc),
                user=UserId("unconfigured"),
                uname="unconfigured",
            ),
            load_catalog(),
            refresher=GatewayTokenRefresher(credential_exchange),
            settings=AdapterSettings(
                refresh_threshold_seconds=settings.mes_token_refresh_threshold_seconds,
                timestamp_ttl_seconds=settings.mes_timestamp_ttl_seconds,
            ),
            recorder=mes_recorder,
            tenant_registry=tenant_registry,
        )
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

    clock = supplied.clock or SystemClock()
    capability_runner = _build_capability_runner(supplied, mes, settings)
    artifact_store, exporter = _build_export_service(supplied, settings, clock)

    if supplied.interactions is not None:
        interactions: InteractionStore | None = supplied.interactions
        interactions_status = "fake"
    elif settings.postgres_url is not None:
        engine = create_session_engine(str(settings.postgres_url))
        interactions = SqlInteractionStore(engine, metering=SqlMeteringStore(engine))
        interactions_status = "configured"
    else:
        interactions = None
        interactions_status = "not_configured"

    readiness = {
        "identity": (
            "configured"
            if credential_exchange is not None
            else ("fake" if supplied.identity is not None else "not_configured")
        ),
        "mes": mes_status,
        "model": model_status,
        "sessions": "fake" if supplied.sessions is not None else "not_configured",
        "artifacts": "fake" if supplied.artifacts is not None else "not_configured",
        "interactions": interactions_status,
        "postgres": "configured" if settings.postgres_url is not None else "not_configured",
        "litellm": model_status,
        "redis": "configured" if settings.redis_url is not None else "not_configured",
        "export": "configured" if exporter is not None else "not_configured",
    }
    directory: MesDirectorySource | CachedDirectorySource | None = (
        MesDirectorySource(mes, load_catalog()) if isinstance(mes, HongzhaoMesAdapter) else None
    )
    # Base-data caching: the full-roster department/employee lookups are
    # role-independent, so they are cached under a shared (scope-free) key and
    # reused across roles. Built before the membership/authorization wiring so
    # both the directory resolver and the organization source share the cache.
    cache = _build_cache(settings)
    if directory is not None and cache is not None:
        directory = CachedDirectorySource(directory, cache)
    # Membership comes from the token gateway when it is configured: the
    # authoritative role and bound departments are token fields, so
    # authorization completes before any business-data call.
    membership_source = (
        TokenBackedMembershipResolver(credential_exchange)
        if credential_exchange is not None
        else _UnresolvedMemberships()
    )
    authorization = supplied.authorization or AuthorizationService(
        memberships=membership_source,
        organizations=directory or _UnresolvedOrganizations(),
        versions=FixedScopeVersionAssigner(),
    )
    business_filters = BusinessFilterResolver(directory) if directory is not None else None
    personalization = _build_personalization(supplied, settings, clock)
    return ApplicationContainer(
        settings=settings,
        capabilities=CapabilityRegistry(),
        identity=supplied.identity or NotConfiguredIdentityProvider(),
        mes=mes,
        model=model,
        sessions=supplied.sessions or NotConfiguredSessionRepository(),
        artifacts=artifact_store or supplied.artifacts or NotConfiguredArtifactStore(),
        clock=clock,
        authorization=authorization,
        audit=supplied.audit or InMemoryAuditSink(),
        interactions=interactions,
        capability_runner=capability_runner,
        artifact_exporter=exporter,
        personalization=personalization,
        cache=cache,
        credential_exchange=credential_exchange,
        sessions_service=_build_session_service(
            settings,
            supplied,
            interactions,
            authorization,
            clock,
            model,
            capability_runner,
            exporter,
            business_filters,
            personalization,
            credential_exchange,
        ),
        readiness=readiness,
    )


def _build_cache(settings: FactoryAgentSettings) -> AuthAwareCache | None:
    """Authorization-aware Redis cache; absent when Redis is not configured.

    Redis is only an optimization: the cache falls back to the source of truth
    on any store error, and every key is bound to the tenant and an irreversible
    scope fingerprint (``scope_version``).
    """
    if settings.redis_url is None:
        return None
    store = RedisCacheStore(str(settings.redis_url))
    return AuthAwareCache(
        store,
        contract_version="mes-contract-v2",
        metric_version="metric-registry-v1",
        data_version="mock-mes-v20260821",
    )


def _build_personalization(
    supplied: DependencyOverrides,
    settings: FactoryAgentSettings,
    clock: Clock,
) -> PersonalizationService | None:
    """Compose history/favorites/user-mapping over PostgreSQL when available."""
    if supplied.personalization is not None:
        return supplied.personalization
    if settings.postgres_url is None:
        return None
    engine = create_session_engine(str(settings.postgres_url))
    return PersonalizationService(
        SqlHistoryRepository(engine),
        SqlFavoriteRepository(engine),
        SqlUserMappingRepository(engine),
        clock=clock.now,
    )


def _build_scope_violation_store(
    settings: FactoryAgentSettings,
) -> SqlScopeViolationStore | None:
    """Durable review surface for role-consistency findings (Story 2)."""
    if settings.postgres_url is None:
        return None
    engine = create_session_engine(str(settings.postgres_url))
    return SqlScopeViolationStore(engine)


def _build_session_service(
    settings: FactoryAgentSettings,
    supplied: DependencyOverrides,
    interactions: InteractionStore | None,
    authorization: AuthorizationService,
    clock: Clock,
    model: ModelGateway,
    capability_runner: CapabilityRunner | None,
    exporter: ArtifactExporter | None,
    business_filters: BusinessFilterResolver | None,
    personalization: PersonalizationService | None = None,
    credential_exchange: TokenCredentialExchange | None = None,
) -> SessionService | None:
    """Only compose the session pipeline when its dependencies exist."""
    if interactions is None or capability_runner is None:
        return None
    parser = CapabilityIntentParser(
        model,
        supplied.capability_catalog or default_capability_catalog(),
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
        capability_runner,
        clock,
        new_id=supplied.new_id or (lambda: uuid4().hex),
        narrower=FilterNarrower(),
        business_filters=business_filters,
        limits=SessionLimits(
            max_input_chars=settings.session_max_input_chars,
            max_clarification_rounds=settings.session_max_clarification_rounds,
            heartbeat_seconds=settings.session_heartbeat_seconds,
        ),
        exporter=exporter,
        personalization=personalization,
        credential_binder=credential_exchange,
        time_range_max_days=settings.time_range_max_days,
        validator=ConsistencyValidator(),
        violations=_build_scope_violation_store(settings),
        audit=supplied.audit or InMemoryAuditSink(),
        validation_mode=settings.validation_mode,
    )


def _build_capability_runner(
    supplied: DependencyOverrides,
    mes: MesDataSource[Any, Any],
    settings: FactoryAgentSettings,
) -> CapabilityRunner | None:
    """Compose the reviewed kernel runner over a real Hongzhao adapter only.

    Injected fakes always win; only a real ``HongzhaoMesAdapter`` builds the
    full kernel pipeline (recipe → executor → sandbox → ResultTable).
    ``resource_columns`` lets an empty fan-out (FR-009 call-budget exhaustion)
    still register a typed sandbox table for downstream local compute.
    """
    if supplied.capability_runner is not None:
        return supplied.capability_runner
    if not isinstance(mes, HongzhaoMesAdapter):
        return None
    catalog = load_catalog()
    recipes = load_recipes(catalog.operation_ids)
    executor = ScopedExecutor(adapter=mes, catalog=catalog)
    resource_columns: dict[str, tuple[str, ...]] = {}
    base_data_operations: set[str] = set()
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        model = ROW_MODEL_BY_RESOURCE.get(operation.resource) if operation.resource else None
        resource_columns[operation_id] = tuple(model.model_fields) if model else ()
        if operation.resource in BASE_DATA_RESOURCES:
            base_data_operations.add(operation_id)
    return KernelCapabilityRunner(
        executor,
        recipes,
        default_metric_registry(),
        settings=KernelSettings(
            delivery_warning_ratio_percent=settings.delivery_warning_ratio_percent,
            delivery_warning_fallback_days=settings.delivery_warning_fallback_days,
        ),
        resource_columns=resource_columns,
        base_data_operations=frozenset(base_data_operations),
    )


def _build_export_service(
    supplied: DependencyOverrides,
    settings: FactoryAgentSettings,
    clock: Clock,
) -> tuple[ArtifactStore | None, ArtifactExporter | None]:
    """Compose the instant no-retention exporter (Story 3).

    The exporter is purely in-memory (transient buffer): it needs no object
    store and no PostgreSQL. It is built whenever an injected override is
    absent, so generated exports are always downloadable within the short
    buffer window for any configured deployment.
    """
    if supplied.artifact_exporter is not None:
        return supplied.artifacts, supplied.artifact_exporter
    exporter = ExportService(
        clock=clock.now,
        ttl_seconds=settings.export_buffer_ttl_seconds,
        max_entries=settings.export_buffer_max_entries,
    )
    return supplied.artifacts, exporter


def _load_registry(settings: FactoryAgentSettings) -> ModelRegistry | None:
    """A missing or invalid registry degrades readiness instead of crashing startup."""
    try:
        return load_model_registry(settings.model_registry_path)
    except MesError:
        return None


class _UnresolvedMemberships:
    """Placeholder until the credential-bundle resolver is wired."""

    async def resolve(self, credential: TrustedCredential, as_of: datetime):
        raise DependencyNotConfiguredError("membership resolver is not configured")


class _UnresolvedOrganizations:
    async def list_current_depts(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
    ) -> tuple[DeptId, ...]:
        raise DependencyNotConfiguredError("organization source is not configured")
