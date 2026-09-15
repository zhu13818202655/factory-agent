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
from factory_agent.application.chitchat import ChatResponder
from factory_agent.application.consistency import ConsistencyValidator
from factory_agent.application.filters import FilterNarrower
from factory_agent.application.intent import CapabilityCatalog, CapabilityIntentParser
from factory_agent.application.personal import PersonalizationService
from factory_agent.application.preferences import PreferencesService
from factory_agent.application.push_channel import LocalPushChannel
from factory_agent.application.reporting import DirectReportRunner, ReportingService
from factory_agent.application.scope_guard import ScopeGuard
from factory_agent.application.session import SessionLimits, SessionService
from factory_agent.application.summary import ResultSummarizer
from factory_agent.application.usage import ContextVarMesCallRecorder
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.directory import MesDirectorySource
from factory_agent.data_api.hongzhao import AdapterSettings, HongzhaoMesAdapter
from factory_agent.data_api.pagination import PagerBudget
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
from factory_agent.export.local_store import LocalArtifactStore
from factory_agent.export.s3_store import S3ArtifactStore
from factory_agent.export_service import ExportService
from factory_agent.infrastructure.cache import RedisCacheStore
from factory_agent.llm.health import EndpointHealthMonitor, HttpModelsProbe
from factory_agent.llm.registry import ModelRegistry, load_model_registry
from factory_agent.llm.router_gateway import LiteLlmRouterGateway
from factory_agent.observability.audit import AuditSink, StructuredLogAuditSink
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.metering import SqlMeteringStore
from factory_agent.persistence.personal_store import (
    SqlFavoriteRepository,
    SqlHistoryRepository,
    SqlUserMappingRepository,
)
from factory_agent.persistence.push_store import (
    SqlPushDeliveryStore,
    SqlPushPreferenceRepository,
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
from factory_agent.ports.artifacts import ArtifactExporter, ExportStore
from factory_agent.ports.not_configured import (
    DependencyNotConfiguredError,
    NotConfiguredArtifactStore,
    NotConfiguredIdentityProvider,
    NotConfiguredMesDataSource,
    NotConfiguredModelGateway,
    NotConfiguredSessionRepository,
)
from factory_agent.ports.push import PushChannel


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
    preferences_service: PreferencesService | None = None
    reporting: ReportingService | None = None
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
    preferences_service: PreferencesService | None = None
    reporting: ReportingService | None = None
    cache: AuthAwareCache | None = None
    credential_exchange: TokenCredentialExchange | None = None
    model_health: EndpointHealthMonitor | None = None
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
                factory_timezone=settings.factory_timezone,
            ),
            pager_budget=PagerBudget(
                page_size=settings.mes_page_size,
                page_size_ceiling=settings.mes_page_size_max,
                max_pages=settings.mes_max_pages,
                max_rows=settings.mes_max_rows,
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
        model_health: EndpointHealthMonitor | None = None
    else:
        registry = _load_registry(settings)
        model_health = None
        if registry is not None and registry.is_usable():
            gateway_impl = LiteLlmRouterGateway(
                registry,
                default_timeout_seconds=settings.llm_timeout_seconds,
                default_temperature=settings.llm_temperature,
                default_top_p=settings.llm_top_p,
                default_max_output_tokens=settings.llm_max_output_tokens,
                num_retries=settings.llm_num_retries,
                allowed_fails=settings.llm_allowed_fails,
                cooldown_seconds=settings.llm_cooldown_seconds,
                thinking_enabled=settings.llm_thinking_enabled,
                thinking_effort=settings.llm_thinking_effort,
            )
            model = gateway_impl
            model_health = EndpointHealthMonitor(
                registry,
                HttpModelsProbe(timeout_seconds=settings.llm_health_probe_timeout_seconds),
                apply=gateway_impl.use_registry,
                failures_to_demote=settings.llm_health_probe_failures_to_demote,
            )
            model_status = "configured"
        else:
            model = NotConfiguredModelGateway()
            model_status = "not_configured"

    clock = supplied.clock or SystemClock()
    capability_runner = _build_capability_runner(supplied, mes, settings)
    artifact_store, exporter, export_status = _build_export_service(supplied, settings, clock)

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
        "export": export_status,
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
    preferences_service = _build_preferences_service(supplied, settings, clock)
    push_channel = _build_push_channel(supplied, settings)
    reporting = _build_reporting(
        supplied, authorization, capability_runner, business_filters, clock, push_channel
    )
    # One audit sink per process: the fail-closed download gate and the
    # best-effort consistency alert must report to the same sink.
    audit = supplied.audit or StructuredLogAuditSink()
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
        audit=audit,
        interactions=interactions,
        capability_runner=capability_runner,
        artifact_exporter=exporter,
        personalization=personalization,
        preferences_service=preferences_service,
        reporting=reporting,
        cache=cache,
        credential_exchange=credential_exchange,
        model_health=model_health,
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
            audit,
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
        data_version="mes-v20260821",
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
    """Durable review surface for role-consistency findings."""
    if settings.postgres_url is None:
        return None
    engine = create_session_engine(str(settings.postgres_url))
    return SqlScopeViolationStore(engine)


def _build_preferences_service(
    supplied: DependencyOverrides,
    settings: FactoryAgentSettings,
    clock: Clock,
) -> PreferencesService:
    """Push preferences service; a no-PG deployment returns defaults only."""
    if supplied.preferences_service is not None:
        return supplied.preferences_service
    repository = (
        SqlPushPreferenceRepository(create_session_engine(str(settings.postgres_url)))
        if settings.postgres_url is not None
        else None
    )
    return PreferencesService(repository, clock=clock)


def _build_push_channel(
    supplied: DependencyOverrides,
    settings: FactoryAgentSettings,
) -> PushChannel | None:
    """Local fake push channel with an envelope delivery log when PostgreSQL."""
    if settings.postgres_url is None:
        return None
    return LocalPushChannel(SqlPushDeliveryStore(create_session_engine(str(settings.postgres_url))))


def _build_reporting(
    supplied: DependencyOverrides,
    authorization: AuthorizationService,
    runner: CapabilityRunner | None,
    business_filters: BusinessFilterResolver | None,
    clock: Clock,
    channel: PushChannel | None,
) -> ReportingService | None:
    """Morning-report/summary generation needs a real capability runner."""
    if supplied.reporting is not None:
        return supplied.reporting
    if runner is None:
        return None
    direct = DirectReportRunner(
        authorization, runner, clock=clock, business_filters=business_filters
    )
    return ReportingService(direct, channel, clock=clock)


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
    audit: AuditSink | None = None,
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
        time_range_max_days=settings.time_range_max_days,
        time_parse_mode=settings.time_parse_mode,
        scope_guard_mode=settings.scope_guard_mode,
    )
    chat = ChatResponder(
        model,
        model_alias=settings.llm_summary_alias,
        max_history_turns=settings.session_history_max_turns,
        max_history_chars=settings.session_history_max_chars,
    )
    summarizer = ResultSummarizer(model, model_alias=settings.llm_summary_alias)
    scope_guard = ScopeGuard(model, model_alias=settings.llm_summary_alias)
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
            follow_timeout_seconds=settings.session_follow_timeout_seconds,
            stale_running_seconds=settings.session_stale_running_seconds,
            run_timeout_seconds=settings.session_run_timeout_seconds,
            abandoned_pending_seconds=settings.session_abandoned_pending_seconds,
        ),
        exporter=exporter,
        personalization=personalization,
        credential_binder=credential_exchange,
        time_range_max_days=settings.time_range_max_days,
        factory_timezone=settings.factory_timezone,
        validator=ConsistencyValidator(),
        violations=_build_scope_violation_store(settings),
        audit=audit,
        chat=chat,
        summarizer=summarizer,
        scope_guard=scope_guard,
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
            fanout_concurrency=settings.mes_fanout_concurrency,
        ),
        resource_columns=resource_columns,
        base_data_operations=frozenset(base_data_operations),
    )


def _build_export_service(
    supplied: DependencyOverrides,
    settings: FactoryAgentSettings,
    clock: Clock,
) -> tuple[ArtifactStore | None, ArtifactExporter | None, str]:
    """Compose the retained exporter over the configured artifact backend.

    Exports persist to the S3-compatible object store when ``s3_endpoint_url``
    is set and to a local directory otherwise, and stay downloadable across
    restarts within ``export_retention_seconds`` either way. The exporter is
    built whenever an injected override is absent, so every configured
    deployment can generate downloads; the returned label is what readiness
    reports as the live backend.
    """
    if supplied.artifact_exporter is not None:
        return supplied.artifacts, supplied.artifact_exporter, "fake"
    store, backend = _build_artifact_store(settings)
    exporter = ExportService(
        store=store,
        clock=clock.now,
        retention_seconds=settings.export_retention_seconds,
        max_entries=settings.export_buffer_max_entries,
    )
    return supplied.artifacts, exporter, backend


def _build_artifact_store(settings: FactoryAgentSettings) -> tuple[ExportStore, str]:
    """Select the artifact backend; the label names it in the readiness report."""
    endpoint = settings.s3_endpoint_url.strip()
    if endpoint:
        return (
            S3ArtifactStore(
                endpoint_url=endpoint,
                bucket=settings.s3_bucket,
                access_key=settings.s3_access_key.get_secret_value(),
                secret_key=settings.s3_secret_key.get_secret_value(),
                region=settings.s3_region,
                path_style=settings.s3_path_style,
            ),
            "s3",
        )
    return LocalArtifactStore(settings.export_store_dir), "local"


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
