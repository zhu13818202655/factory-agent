from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
import pytest

from factory_agent.application.cache import AuthAwareCache
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import CURRENT_BUNDLE
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.data_api.result_cache import (
    FetchThrough,
    ResultCacheExecutor,
    ResultCacheMode,
    V2ResultCache,
)
from factory_agent.data_api.token_gateway import (
    GatewayTokenRefresher,
    TokenCredentialExchange,
    current_bundle,
)
from factory_agent.domain import NarrowedFilters, Role, TenantId, UserId
from factory_agent.domain.errors import ForbiddenError, UnauthenticatedError
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor
from factory_agent.ports.contracts import TrustedCredential
from tests.support.cache import InMemoryCacheStore

NOW = datetime(2026, 10, 12, 6, tzinfo=timezone.utc)
APP_KEY = "synthetic-refresh-factory"
USER = "synthetic-refresh-user"
DEPT_A = "synthetic-refresh-dept-a"
DEPT_B = "synthetic-refresh-dept-b"
ENCRYPTED = "synthetic-refresh-encrypted"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class RefreshScenario:
    def __init__(self, changes: dict[str, Any], reactive: bool) -> None:
        self.changes = changes
        self.reactive = reactive
        self.token_calls = 0
        self.business_calls = 0

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system/token":
            self.token_calls += 1
            short_lived = self.token_calls == 1 and not self.reactive
            duration = timedelta(seconds=60) if short_lived else timedelta(hours=2)
            result: dict[str, Any] = {
                "tokenType": "Bearer",
                "accessToken": f"synthetic-refresh-access-{self.token_calls}",
                "expiresIn": int(duration.total_seconds()),
                "expiresAt": (NOW + duration).isoformat(),
                "user": USER,
                "uname": "synthetic-refresh-name",
                "dept": DEPT_A,
                "manageDept": f"{DEPT_A},{DEPT_B}",
                "appkey": APP_KEY,
                "sign": f"synthetic-refresh-sign-{self.token_calls}",
                "timestamp": self.token_calls,
                "roles": ["99"],
            }
            if self.token_calls > 1:
                result.update(self.changes)
            return httpx.Response(
                200, json={"code": 1, "message": "success", "timestamp": 1, "result": result}
            )
        self.business_calls += 1
        if self.reactive and self.business_calls == 1:
            # Exercise the adapter's unified expired-request refresh signal.
            raise UnauthenticatedError()
        assert (
            request.headers["Authorization"]
            == f"Bearer synthetic-refresh-access-{self.token_calls}"
        )
        return httpx.Response(
            200,
            json={
                "code": 1,
                "message": "success",
                "timestamp": 1,
                "result": {
                    "list": [{"inputdate": "2026-07-01", "uid": USER, "sl": "7"}],
                    "total": 1,
                },
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reactive", [False, True])
@pytest.mark.parametrize(
    "changes,identity_changed",
    [
        ({"appkey": "synthetic-refresh-other-factory"}, True),
        ({"user": "synthetic-refresh-other-user"}, True),
        ({"roles": ["02"]}, False),
        ({"manageDept": DEPT_B}, False),
        ({"dept": DEPT_B}, False),
    ],
    ids=["tenant", "user", "role", "bound-departments", "home-department"],
)
async def test_changed_refresh_identity_or_scope_cannot_retry_or_populate_owner_cache(
    changes: dict[str, Any], identity_changed: bool, reactive: bool
) -> None:
    scenario = RefreshScenario(changes, reactive)
    transport = httpx.MockTransport(scenario.respond)
    token_client = httpx.AsyncClient(base_url="http://refresh.invalid", transport=transport)
    business_client = httpx.AsyncClient(base_url="http://refresh.invalid", transport=transport)
    exchange = TokenCredentialExchange(
        "http://refresh.invalid", client=token_client, clock=FixedClock()
    )
    principal = await exchange.authenticate(ENCRYPTED)
    original = exchange.bundle_for(principal.credential)
    assert original is not None
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter(
        "http://refresh.invalid",
        original,
        catalog,
        refresher=GatewayTokenRefresher(exchange),
        client=business_client,
        clock=FixedClock(),
    )
    store = InMemoryCacheStore()
    cache = V2ResultCache(
        AuthAwareCache(
            store, contract_version="fixture", metric_version="fixture", data_version="fixture"
        ),
        mode=ResultCacheMode.READWRITE,
        clock=lambda: NOW,
    )
    executor = ResultCacheExecutor(cast("FetchThrough", ScopedExecutor(adapter, catalog)), cache)
    request = ExecutionRequest(
        "GongziClQuery",
        (datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc)),
        role=Role.OWNER,
    )
    try:
        with exchange.bind_for(principal.credential):
            with pytest.raises(ForbiddenError) as caught:
                await executor.execute_full_step(
                    NarrowedFilters(principal.credential.tenant_id, None, None),
                    request,
                    extra_params={"Flag": "0", "Type": "0,1,2"},
                )
            assert current_bundle() is original
            for canary in (APP_KEY, USER, DEPT_A, DEPT_B, ENCRYPTED):
                assert canary not in str(caught.value)
        assert scenario.token_calls == 2
        assert scenario.business_calls == int(reactive)
        assert store.values == {}
        if identity_changed:
            assert exchange.principal_for(principal.credential) is principal
            assert exchange.bundle_for(principal.credential) is original
            other = TrustedCredential(
                TenantId(str(changes.get("appkey", APP_KEY))),
                UserId(str(changes.get("user", USER))),
            )
            assert exchange.principal_for(other) is None
            assert exchange.bundle_for(other) is None
        else:
            next_principal = await exchange.authenticate(ENCRYPTED)
            assert scenario.token_calls == 2
            assert next_principal.credential == principal.credential
            assert next_principal.role is (Role.MANAGER if "roles" in changes else Role.OWNER)
            next_bundle = exchange.bundle_for(next_principal.credential)
            assert next_bundle is not None and next_bundle is not original
            with exchange.bind_for(next_principal.credential):
                assert current_bundle() is next_bundle
    finally:
        await adapter.aclose()
        await exchange.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reactive", [False, True])
async def test_stable_scope_refresh_accepts_department_set_reordering_and_writes_complete_cache(
    reactive: bool,
) -> None:
    scenario = RefreshScenario({"manageDept": f"{DEPT_B},{DEPT_A},{DEPT_A}"}, reactive)
    transport = httpx.MockTransport(scenario.respond)
    token_client = httpx.AsyncClient(base_url="http://refresh.invalid", transport=transport)
    business_client = httpx.AsyncClient(base_url="http://refresh.invalid", transport=transport)
    exchange = TokenCredentialExchange(
        "http://refresh.invalid", client=token_client, clock=FixedClock()
    )
    principal = await exchange.authenticate(ENCRYPTED)
    original = exchange.bundle_for(principal.credential)
    assert original is not None
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter(
        "http://refresh.invalid",
        original,
        catalog,
        refresher=GatewayTokenRefresher(exchange),
        client=business_client,
        clock=FixedClock(),
    )
    store = InMemoryCacheStore()
    executor = ResultCacheExecutor(
        cast("FetchThrough", ScopedExecutor(adapter, catalog)),
        V2ResultCache(
            AuthAwareCache(
                store, contract_version="fixture", metric_version="fixture", data_version="fixture"
            ),
            mode=ResultCacheMode.READWRITE,
            clock=lambda: NOW,
        ),
    )
    try:
        with exchange.bind_for(principal.credential):
            result = await executor.execute_full_step(
                NarrowedFilters(principal.credential.tenant_id, None, None),
                ExecutionRequest(
                    "GongziClQuery",
                    (
                        datetime(2026, 7, 1, tzinfo=timezone.utc),
                        datetime(2026, 7, 2, tzinfo=timezone.utc),
                    ),
                    role=Role.OWNER,
                ),
                extra_params={"Flag": "0", "Type": "0,1,2"},
            )
            assert current_bundle() is exchange.bundle_for(principal.credential)
            assert current_bundle() is not original
        assert result.complete
        assert result.rows[0]["sl"] == "7"
        assert scenario.token_calls == 2
        assert scenario.business_calls == 1 + int(reactive)
        assert len(store.values) == 1
    finally:
        await adapter.aclose()
        await exchange.aclose()


@pytest.mark.asyncio
async def test_stable_refresh_rebinds_even_if_another_interaction_already_updated_the_entry() -> (
    None
):
    scenario = RefreshScenario({}, reactive=True)
    client = httpx.AsyncClient(
        base_url="http://refresh.invalid", transport=httpx.MockTransport(scenario.respond)
    )
    exchange = TokenCredentialExchange("http://refresh.invalid", client=client, clock=FixedClock())
    try:
        principal = await exchange.authenticate(ENCRYPTED)
        with exchange.bind_for(principal.credential):
            original = current_bundle()
            temporary = CURRENT_BUNDLE.set(None)
            try:
                await exchange.refresh_bundle(
                    principal.credential.tenant_id, principal.credential.user_id
                )
            finally:
                CURRENT_BUNDLE.reset(temporary)
            assert current_bundle() is original
            latest = await GatewayTokenRefresher(exchange).refresh()
            assert current_bundle() is latest
            assert latest is exchange.bundle_for(principal.credential)
        assert scenario.token_calls == 3
        assert scenario.business_calls == 0
    finally:
        await exchange.aclose()
