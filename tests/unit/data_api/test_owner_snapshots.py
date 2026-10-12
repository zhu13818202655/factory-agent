import contextlib
import json
from collections.abc import Generator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import SecretStr

import factory_agent.data_api.owner_snapshots as snapshots
from factory_agent.data_api.credentials import CURRENT_BUNDLE, MesCredentialBundle
from factory_agent.data_api.hongzhao import AdapterSettings, TokenRefresher
from factory_agent.data_api.owner_snapshots import (
    OwnerSnapshotConfigurationError,
    OwnerSnapshotFetcher,
    OwnerSnapshotPool,
    OwnerSnapshotUnavailableError,
)
from factory_agent.data_api.pagination import PagerBudget
from factory_agent.data_api.token_gateway import TokenCredentialExchange, current_bundle
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    NarrowedFilters,
    Role,
    ScopeVersion,
    TenantContext,
    TenantId,
    UserId,
    local_calendar_dates,
)
from factory_agent.domain.errors import ForbiddenError, InvalidRequestError, TenantDisabledError
from factory_agent.observability.context import bind_tenant_id, current_tenant_id
from factory_agent.ports import MesCallRecorder
from factory_agent.ports.contracts import ResolvedPrincipal, ResourceFetchResult, TrustedCredential
from factory_agent.ports.tenant_registry import TenantRegistryReader, TenantRegistryRecord

NOW = datetime(2026, 10, 16, 6, tzinfo=timezone.utc)
WINDOW = ("2026-09-01", "2026-09-30")


class FixedClock:
    def now(self) -> datetime:
        return NOW


@dataclass(frozen=True)
class Profile:
    tenant: str = "TENANT-A"
    user: str = "OWNER-A"
    role: Role = Role.OWNER


class FakeExchange(TokenCredentialExchange):
    def __init__(self, profiles: Mapping[str, Profile | Exception]) -> None:
        super().__init__("http://exchange.invalid", clock=FixedClock())
        self.profiles = dict(profiles)
        self.entries: dict[TrustedCredential, tuple[ResolvedPrincipal, MesCredentialBundle]] = {}
        self.authenticate_calls: list[str] = []
        self.refresh_to: Profile | None = None
        self.refresh_calls = 0
        self.update_context_on_refresh = True
        self.close_calls = 0

    def install(self, profile: Profile) -> TrustedCredential:
        credential = TrustedCredential(TenantId(profile.tenant), UserId(profile.user))
        self.entries[credential] = self._entry(profile)
        return credential

    @staticmethod
    def _entry(profile: Profile) -> tuple[ResolvedPrincipal, MesCredentialBundle]:
        credential = TrustedCredential(TenantId(profile.tenant), UserId(profile.user))
        principal = ResolvedPrincipal(
            credential=credential,
            display_name="Synthetic owner",
            role=profile.role,
            bound_dept_ids=(DeptId("DEPT-A"),),
        )
        bundle = MesCredentialBundle(
            access_token="synthetic-access",
            app_key=profile.tenant,
            sign="synthetic-sign",
            timestamp=1,
            expires_at=NOW + timedelta(days=1),
            user=UserId(profile.user),
            uname="Synthetic owner",
            roles=(profile.role.mes_code,),
            dept="DEPT-A",
        )
        return principal, bundle

    async def authenticate(self, encrypted_credential: str) -> ResolvedPrincipal:
        self.authenticate_calls.append(encrypted_credential)
        profile = self.profiles[encrypted_credential]
        if isinstance(profile, Exception):
            raise profile
        credential = self.install(profile)
        return self.entries[credential][0]

    def principal_for(self, credential: TrustedCredential) -> ResolvedPrincipal | None:
        entry = self.entries.get(credential)
        return entry[0] if entry is not None else None

    def bundle_for(self, credential: TrustedCredential) -> MesCredentialBundle | None:
        entry = self.entries.get(credential)
        return entry[1] if entry is not None else None

    @contextlib.contextmanager
    def bind_for(self, credential: TrustedCredential) -> Generator[None, None, None]:
        entry = self.entries[credential]
        token = CURRENT_BUNDLE.set(entry[1])
        try:
            yield
        finally:
            CURRENT_BUNDLE.reset(token)

    async def refresh_bundle(self, tenant_id: TenantId, user_id: UserId) -> MesCredentialBundle:
        self.refresh_calls += 1
        credential = TrustedCredential(tenant_id, user_id)
        profile = self.refresh_to or Profile(str(tenant_id), str(user_id))
        principal, bundle = self._entry(profile)
        self.entries[credential] = principal, bundle
        if self.update_context_on_refresh:
            CURRENT_BUNDLE.set(bundle)
        return bundle

    async def aclose(self) -> None:
        self.close_calls += 1


class Authorizer:
    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange
        self.calls: list[TrustedCredential] = []
        self.context_changes: dict[str, Any] = {}
        self.scope_changes: dict[str, Any] = {}
        self.failure: Exception | None = None

    async def __call__(
        self, credential: TrustedCredential, as_of: datetime
    ) -> tuple[TenantContext, DataScope]:
        self.calls.append(credential)
        bound = current_bundle()
        assert bound is not None
        assert bound.tenant_id == credential.tenant_id
        assert bound.user == credential.user_id
        bind_tenant_id(str(credential.tenant_id))
        if self.failure is not None:
            raise self.failure
        context = TenantContext(
            tenant_id=credential.tenant_id,
            user_id=credential.user_id,
            employee_id=EmployeeId(str(credential.user_id)),
            role=Role.OWNER,
            resolved_at=as_of,
        )
        scope = DataScope(
            tenant_id=credential.tenant_id,
            employee_ids=frozenset({context.employee_id}),
            dept_ids=frozenset({DeptId("DEPT-A")}),
            evaluated_at=as_of,
            scope_version=ScopeVersion("owner-test-scope"),
            mes_filtered=True,
        )
        return replace(context, **self.context_changes), replace(scope, **self.scope_changes)


class Registry:
    def __init__(self, status: str = "active") -> None:
        self.status = status
        self.calls: list[str] = []

    async def get(self, app_key: str) -> TenantRegistryRecord:
        self.calls.append(app_key)
        return TenantRegistryRecord(app_key, "Synthetic tenant", self.status)


class FakeAdapter:
    def __init__(
        self,
        bundle: MesCredentialBundle,
        refresher: TokenRefresher,
        registry: TenantRegistryReader | None,
        recorder: MesCallRecorder | None,
        settings: AdapterSettings,
        budget: PagerBudget,
    ) -> None:
        self.bundle = bundle
        self.refresher = refresher
        self.registry = registry
        self.recorder = recorder
        self.settings = settings
        self.budget = budget
        self.calls: list[
            tuple[str, NarrowedFilters, tuple[datetime, datetime], dict[str, Any]]
        ] = []
        self.bound_users: list[str] = []
        self.bound_bundles: list[MesCredentialBundle] = []
        self.log_tenants: list[str | None] = []
        self.refresh_before_fetch = False
        self.failure: Exception | None = None
        self.closed = 0

    async def fetch_resource(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        assert page_size == self.budget.page_size
        if self.refresh_before_fetch:
            await self.refresher.refresh()
        bound = current_bundle()
        assert bound is not None
        self.bound_users.append(str(bound.user))
        self.bound_bundles.append(bound)
        self.log_tenants.append(current_tenant_id())
        self.calls.append((operation_id, filters, time_range, dict(extra_params or {})))
        if self.failure is not None:
            raise self.failure
        return ResourceFetchResult(
            rows=({"tenant_marker": str(filters.tenant_id)},),
            total=1,
            pages_fetched=1,
            complete=True,
        )

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch) -> list[FakeAdapter]:
    created: list[FakeAdapter] = []

    def build(
        _base_url: str, bundle: MesCredentialBundle, _catalog: object, **kwargs: Any
    ) -> FakeAdapter:
        adapter = FakeAdapter(
            bundle,
            kwargs["refresher"],
            kwargs["tenant_registry"],
            kwargs["recorder"],
            kwargs["settings"],
            kwargs["pager_budget"],
        )
        created.append(adapter)
        return adapter

    monkeypatch.setattr(snapshots, "HongzhaoMesAdapter", build)
    return created


def fetcher(
    exchange: FakeExchange,
    authorize: Authorizer | None = None,
    registry: TenantRegistryReader | None = None,
) -> OwnerSnapshotFetcher:
    return OwnerSnapshotFetcher(
        exchange,
        "encrypted-owner-a",
        "http://mes.invalid",
        authorize=authorize or Authorizer(exchange),
        clock=FixedClock(),
        adapter_settings=AdapterSettings(factory_timezone="Asia/Shanghai"),
        pager_budget=PagerBudget(page_size=123),
        tenant_registry=registry,
    )


def pool(exchange: FakeExchange, configuration: str) -> OwnerSnapshotPool:
    return OwnerSnapshotPool(
        exchange,
        SecretStr(configuration),
        "http://mes.invalid",
        authorize=Authorizer(exchange),
        clock=FixedClock(),
        adapter_settings=AdapterSettings(),
        pager_budget=PagerBudget(),
    )


@pytest.mark.asyncio
async def test_initialize_authorizes_owner_without_business_calls_and_fetches_full_scope(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    authorize = Authorizer(exchange)
    owner = fetcher(exchange, authorize)

    await owner.initialize()

    assert owner.tenant_id() == TenantId("TENANT-A")
    assert authorize.calls == [TrustedCredential(TenantId("TENANT-A"), UserId("OWNER-A"))]
    assert len(adapters) == 1
    assert adapters[0].calls == []
    result = await owner("GongziClQuery", WINDOW, {"Flag": "0", "Type": "0,1,2"})
    assert result.complete is True
    assert result.rows == ({"tenant_marker": "TENANT-A"},)
    assert adapters[0].calls[0][1] == NarrowedFilters(TenantId("TENANT-A"), None, None)
    assert len(authorize.calls) == 2


@pytest.mark.asyncio
async def test_diagnostic_authorization_context_contains_only_verified_identity_and_scope(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    owner = fetcher(exchange)

    context, scope = await owner.authorization_context()

    assert context.role is Role.OWNER
    assert context.tenant_id == scope.tenant_id == owner.tenant_id()
    assert scope.mes_filtered is True
    assert adapters[0].calls == []


@pytest.mark.asyncio
async def test_requested_cross_tenant_owner_is_rejected_before_authorization_or_mes(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    authorize = Authorizer(exchange)
    owner = fetcher(exchange, authorize)

    with pytest.raises(ForbiddenError, match="another tenant"):
        await owner.fetch_for(TenantId("TENANT-B"), "ScjdQuery", WINDOW, {})

    assert authorize.calls == []
    assert adapters == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.EMPLOYEE, Role.GROUP_LEADER, Role.MANAGER])
async def test_non_owner_configuration_performs_zero_business_calls(
    adapters: list[FakeAdapter], role: Role
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile(role=role)})
    authorize = Authorizer(exchange)

    with pytest.raises(ForbiddenError, match="not an owner"):
        await fetcher(exchange, authorize)("ScjdQuery", WINDOW, {})

    assert adapters == []
    assert authorize.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_changes", "scope_changes"),
    [
        ({"role": Role.EMPLOYEE}, {}),
        ({"tenant_id": TenantId("TENANT-B")}, {}),
        ({"user_id": UserId("OTHER")}, {}),
        ({}, {"tenant_id": TenantId("TENANT-B")}),
        ({}, {"mes_filtered": False}),
    ],
)
async def test_inconsistent_authorized_context_or_scope_performs_zero_business_calls(
    adapters: list[FakeAdapter], context_changes: dict[str, Any], scope_changes: dict[str, Any]
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    authorize = Authorizer(exchange)
    authorize.context_changes = context_changes
    authorize.scope_changes = scope_changes

    with pytest.raises(ForbiddenError, match="scope is inconsistent"):
        await fetcher(exchange, authorize)("ScjdQuery", WINDOW, {})

    assert adapters == []


@pytest.mark.asyncio
async def test_snapshot_overrides_and_restores_caller_bundle_and_log_context(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    caller = exchange.install(Profile("TENANT-CALLER", "EMPLOYEE-CALLER", Role.EMPLOYEE))
    owner = fetcher(exchange)
    previous_tenant = current_tenant_id()
    try:
        bind_tenant_id("TENANT-CALLER")
        with exchange.bind_for(caller):
            caller_bundle = current_bundle()
            await owner.initialize()
            assert current_bundle() is caller_bundle
            assert current_tenant_id() == "TENANT-CALLER"
            await owner("ScjdQuery", WINDOW, {})
            assert current_bundle() is caller_bundle
            assert current_tenant_id() == "TENANT-CALLER"
            adapters[0].failure = RuntimeError("synthetic-fetch-failure")
            with pytest.raises(RuntimeError, match="synthetic-fetch-failure"):
                await owner("ScjdQuery", WINDOW, {})
            assert current_bundle() is caller_bundle
            assert current_tenant_id() == "TENANT-CALLER"
    finally:
        bind_tenant_id(previous_tenant)

    assert adapters[0].bound_users == ["OWNER-A", "OWNER-A"]
    assert adapters[0].log_tenants == ["TENANT-A", "TENANT-A"]


@pytest.mark.asyncio
async def test_authorization_failure_restores_caller_context_without_exposing_secret(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    authorize = Authorizer(exchange)
    authorize.failure = RuntimeError("synthetic-sensitive-canary")
    caller = exchange.install(Profile("TENANT-CALLER", "EMPLOYEE-CALLER", Role.EMPLOYEE))
    previous_tenant = current_tenant_id()
    try:
        bind_tenant_id("TENANT-CALLER")
        with exchange.bind_for(caller):
            caller_bundle = current_bundle()
            with pytest.raises(ForbiddenError) as caught:
                await fetcher(exchange, authorize).initialize()
            assert "synthetic-sensitive-canary" not in str(caught.value)
            assert current_bundle() is caller_bundle
            assert current_tenant_id() == "TENANT-CALLER"
    finally:
        bind_tenant_id(previous_tenant)

    assert adapters == []


@pytest.mark.asyncio
async def test_disabled_tenant_performs_zero_authorization_and_business_calls(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    authorize = Authorizer(exchange)
    registry = Registry("disabled")

    with pytest.raises(TenantDisabledError):
        await fetcher(exchange, authorize, registry)("ScjdQuery", WINDOW, {})

    assert registry.calls == ["TENANT-A"]
    assert authorize.calls == []
    assert adapters == []


@pytest.mark.asyncio
async def test_suspend_after_initialize_is_checked_before_the_next_snapshot(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    registry = Registry()
    owner = fetcher(exchange, registry=registry)
    await owner.initialize()
    registry.status = "disabled"

    with pytest.raises(TenantDisabledError):
        await owner("ScjdQuery", WINDOW, {})

    assert adapters[0].calls == []


@pytest.mark.asyncio
async def test_snapshot_calendar_interval_uses_factory_zone_and_half_open_bounds(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    await fetcher(exchange)("ScjdQuery", WINDOW, {})

    start, end = adapters[0].calls[0][2]
    assert start.isoformat() == "2026-09-01T00:00:00+08:00"
    assert end.isoformat() == "2026-10-01T00:00:00+08:00"
    assert start.tzinfo is not None
    assert local_calendar_dates(start, end, start.tzinfo) == WINDOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [Profile("TENANT-B"), Profile(user="OWNER-B"), Profile(role=Role.EMPLOYEE)]
)
async def test_refreshed_tenant_user_or_role_is_rejected_before_business_retry(
    adapters: list[FakeAdapter], change: Profile
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    owner = fetcher(exchange)
    await owner.initialize()
    exchange.refresh_to = change
    adapters[0].refresh_before_fetch = True

    with pytest.raises(ForbiddenError):
        await owner("ScjdQuery", WINDOW, {})

    assert exchange.refresh_calls == 1
    assert adapters[0].calls == []


@pytest.mark.asyncio
async def test_valid_refresh_reauthorizes_and_preserves_caller_binding(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    authorize = Authorizer(exchange)
    owner = fetcher(exchange, authorize)
    await owner.initialize()
    adapters[0].refresh_before_fetch = True
    caller = exchange.install(Profile("TENANT-A", "EMPLOYEE-CALLER", Role.EMPLOYEE))

    with exchange.bind_for(caller):
        caller_bundle = current_bundle()
        result = await owner("ScjdQuery", WINDOW, {})
        assert current_bundle() is caller_bundle

    assert result.complete is True
    assert exchange.refresh_calls == 1
    assert len(authorize.calls) == 3
    assert adapters[0].bound_users == ["OWNER-A"]


@pytest.mark.asyncio
async def test_valid_owner_refresh_rebinds_when_exchange_does_not_update_current_context(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    exchange.update_context_on_refresh = False
    owner = fetcher(exchange)
    await owner.initialize()
    adapters[0].refresh_before_fetch = True
    caller = exchange.install(Profile("TENANT-CALLER", "EMPLOYEE-CALLER", Role.EMPLOYEE))
    previous_tenant = current_tenant_id()
    try:
        bind_tenant_id("TENANT-CALLER")
        with exchange.bind_for(caller):
            caller_bundle = current_bundle()
            result = await owner("ScjdQuery", WINDOW, {})
            assert current_bundle() is caller_bundle
            assert current_tenant_id() == "TENANT-CALLER"
    finally:
        bind_tenant_id(previous_tenant)

    credential = TrustedCredential(TenantId("TENANT-A"), UserId("OWNER-A"))
    assert result.complete
    assert exchange.refresh_calls == 1
    assert adapters[0].bound_bundles[0] is exchange.bundle_for(credential)
    assert adapters[0].bound_bundles[0] is not adapters[0].bundle


@pytest.mark.asyncio
async def test_changed_identity_between_snapshots_does_not_retarget_a_prepared_fetcher(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})
    owner = fetcher(exchange)
    await owner.initialize()
    exchange.profiles["encrypted-owner-a"] = Profile("TENANT-B")

    with pytest.raises(ForbiddenError, match="identity changed"):
        await owner("ScjdQuery", WINDOW, {})

    assert owner.tenant_id() == TenantId("TENANT-A")
    assert adapters[0].calls == []


@pytest.mark.asyncio
async def test_multiple_credentials_isolate_failed_entries_and_target_the_requested_tenant(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange(
        {
            "broken-secret-canary": RuntimeError("broken-secret-canary"),
            "encrypted-owner-a": Profile(),
            "encrypted-owner-b": Profile("TENANT-B", "OWNER-B"),
        }
    )
    owners = pool(exchange, json.dumps(list(exchange.profiles)))

    result = await owners(TenantId("TENANT-B"), "ScjdQuery", WINDOW, {})

    assert len(owners.fetchers) == 3
    assert result.rows == ({"tenant_marker": "TENANT-B"},)
    assert len(adapters) == 1
    assert adapters[0].bound_users == ["OWNER-B"]
    assert owners.fetchers[1].tenant_id() is None
    assert owners.fetchers[2].tenant_id() == TenantId("TENANT-B")
    with pytest.raises(OwnerSnapshotUnavailableError) as caught:
        await owners(TenantId("TENANT-NOT-CONFIGURED"), "ScjdQuery", WINDOW, {})
    assert "broken-secret-canary" not in str(caught.value)
    assert len(adapters[0].calls) == 1


def test_empty_single_and_json_credential_configuration_use_the_existing_reference() -> None:
    exchange = FakeExchange({})
    assert pool(exchange, "").fetchers == ()
    assert pool(exchange, "[]").fetchers == ()
    assert len(pool(exchange, "encrypted-owner-a").fetchers) == 1
    assert len(pool(exchange, '["encrypted-owner-a", "encrypted-owner-b"]').fetchers) == 2
    assert len(pool(exchange, '["encrypted-owner-a", "encrypted-owner-a"]').fetchers) == 1


@pytest.mark.parametrize(
    "configuration", ['["secret-canary",', '{"secret-canary":1}', '["",1]', '"secret-canary"']
)
def test_malformed_secret_configuration_is_rejected_without_exposing_content(
    configuration: str,
) -> None:
    with pytest.raises(OwnerSnapshotConfigurationError) as caught:
        pool(FakeExchange({}), configuration)

    assert str(caught.value) == "owner snapshot credential configuration is invalid"
    assert "secret-canary" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"uid": "EMP-X"},
        {"dept": "DEPT-X"},
        {"groupbyFields": []},
        {"jhdh": "PLAN-X"},
        {"Flag": ["0"]},
        {"Type": "4"},
    ],
)
async def test_snapshot_filters_and_grouping_are_rejected_before_any_exchange(
    adapters: list[FakeAdapter], params: dict[str, Any]
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})

    with pytest.raises(InvalidRequestError):
        await fetcher(exchange)("GongziClQuery", WINDOW, params)

    assert exchange.authenticate_calls == []
    assert adapters == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window",
    [("2026-09-30", "2026-09-01"), ("2026-02-30", "2026-03-01"), ("20260901", "2026-09-30")],
)
async def test_invalid_calendar_window_performs_zero_exchange_and_business_calls(
    adapters: list[FakeAdapter], window: tuple[str, str]
) -> None:
    exchange = FakeExchange({"encrypted-owner-a": Profile()})

    with pytest.raises(InvalidRequestError, match="calendar window is invalid"):
        await fetcher(exchange)("ScjdQuery", window, {})

    assert exchange.authenticate_calls == []
    assert adapters == []


@pytest.mark.asyncio
async def test_pool_closes_only_owned_adapters_and_never_shared_exchange(
    adapters: list[FakeAdapter],
) -> None:
    exchange = FakeExchange(
        {"encrypted-owner-a": Profile(), "encrypted-owner-b": Profile("TENANT-B", "OWNER-B")}
    )
    owners = pool(exchange, '["encrypted-owner-a", "encrypted-owner-b"]')
    for owner in owners.fetchers:
        await owner.initialize()

    await owners.aclose()
    await owners.aclose()

    assert [adapter.closed for adapter in adapters] == [1, 1]
    assert exchange.close_calls == 0
    with pytest.raises(OwnerSnapshotUnavailableError):
        await owners.fetchers[0]("ScjdQuery", WINDOW, {})
