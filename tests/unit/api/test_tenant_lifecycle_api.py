"""Tenant lifecycle at the business edge.

A suspended factory must be refused before any work happens — no intent parse,
no model call, no capability run, no stored conversation — and every registry
failure must fail *open*: a platform ledger outage degrades to "answer
normally", never to "the whole factory is down".

The guard is a router-level dependency, so it is asserted on every business
router rather than on one representative endpoint.
"""

from datetime import datetime, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.tenant_lifecycle import TENANT_DISABLED_DETAIL, TenantLifecycle
from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import Role
from factory_agent.ports.tenant_registry import TenantRegistryRecord
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
    SequentialIds,
)
from tests.support.token import FakeCredentialExchange, principal

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
CREDENTIAL_HEADER = "X-Factory-Credential"
CREDENTIAL = "enc-tenant-a-user-a"
HEADERS = {CREDENTIAL_HEADER: CREDENTIAL}
INTENT_PAYLOAD = (
    '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
)

#: One endpoint per guarded router (sessions / exports / personal / push).
BUSINESS_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/sessions/s-1/messages"),
    ("POST", "/v1/sessions/s-1/interactions"),
    ("GET", "/v1/history"),
    ("GET", "/v1/favorites"),
    ("POST", "/v1/users/me/mapping"),
    ("GET", "/v1/artifacts/a-1/download"),
    ("GET", "/v1/push/preferences"),
)
#: The statistics surface is deliberately *not* a business router: platform
#: operators must still read a suspended factory's history and audit trail.
STATISTICS_ROUTE = "/v1/statistics/tenants/registry"


class FakeReader:
    def __init__(self, records: dict[str, TenantRegistryRecord], *, fail: bool = False) -> None:
        self.records = records
        self.fail = fail
        self.reads = 0

    async def get(self, app_key: str) -> TenantRegistryRecord | None:
        self.reads += 1
        if self.fail:
            raise RuntimeError("registry is unavailable")
        return self.records.get(app_key)


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[str] = []

    async def register_seen(self, app_key: str) -> str | None:
        self.seen.append(app_key)
        if self.fail:
            raise RuntimeError("registration write failed")
        return None


class RecordingAlerts:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def alert(self, kind: str, detail: dict[str, object]) -> None:
        self.kinds.append(kind)


def suspended(app_key: str) -> TenantRegistryRecord:
    return TenantRegistryRecord(app_key=app_key, tenant_name="停用工厂", status="disabled")


class Harness:
    """One assembled app plus the fakes the assertions read back."""

    def __init__(self, lifecycle: TenantLifecycle) -> None:
        self.store = InMemoryInteractionStore()
        self.runner = RecordingCapabilityRunner()
        self.model = ScriptedModelGateway(contents=[INTENT_PAYLOAD])
        self.exchange = FakeCredentialExchange(
            {CREDENTIAL: principal(tenant_id="tenant-a", user_id="user-a", role=Role.EMPLOYEE)}
        )
        self.overrides = DependencyOverrides(
            model=self.model,
            clock=FrozenClock(NOW),
            credential_exchange=self.exchange,
            authorization=AuthorizationService(
                memberships=FakeMembershipSource(
                    memberships_by_credential={
                        ("tenant-a", "user-a"): membership(
                            "user-a", "tenant-a", "emp-1", Role.EMPLOYEE
                        )
                    }
                ),
                organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
                versions=FixedScopeVersionAssigner(),
            ),
            interactions=self.store,
            capability_runner=self.runner,
            new_id=SequentialIds(),
            tenant_lifecycle=lifecycle,
        )
        app = create_app(FactoryAgentSettings(environment="local"), self.overrides)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), BUSINESS_ROUTES)
async def test_suspended_tenant_is_refused_on_every_business_router(method: str, path: str) -> None:
    harness = Harness(TenantLifecycle(reader=FakeReader({"tenant-a": suspended("tenant-a")})))
    async with harness.client:
        response = await harness.client.request(method, path, headers=HEADERS, json={})

    assert response.status_code == 403
    # The refusal carries a stable machine-readable code, not prose.
    assert TENANT_DISABLED_DETAIL in response.text
    # Zero work: no model call, no capability run, and nothing persisted.
    assert harness.model.requests == []
    assert harness.runner.requests == []
    assert harness.store.interactions == {}


@pytest.mark.asyncio
async def test_suspended_tenant_question_never_reaches_the_model() -> None:
    """The question path is the one that actually costs money."""
    harness = Harness(TenantLifecycle(reader=FakeReader({"tenant-a": suspended("tenant-a")})))
    async with harness.client:
        response = await harness.client.post(
            "/v1/sessions/s-1/interactions",
            headers=HEADERS,
            json={"input": "我上个月产量多少"},
        )

    assert response.status_code == 403
    assert harness.model.requests == []
    assert harness.runner.requests == []


@pytest.mark.asyncio
async def test_unknown_tenant_is_admitted() -> None:
    harness = Harness(TenantLifecycle(reader=FakeReader({})))
    async with harness.client:
        response = await harness.client.get("/v1/push/preferences", headers=HEADERS)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_registry_outage_fails_open_and_alerts() -> None:
    alerts = RecordingAlerts()
    harness = Harness(TenantLifecycle(reader=FakeReader({}, fail=True), alerts=alerts))
    async with harness.client:
        response = await harness.client.get("/v1/push/preferences", headers=HEADERS)

    assert response.status_code == 200
    assert alerts.kinds == ["tenant.registry_read_failed"]


@pytest.mark.asyncio
async def test_registration_write_failure_fails_open_and_alerts() -> None:
    alerts = RecordingAlerts()
    writer = FakeWriter(fail=True)
    harness = Harness(
        TenantLifecycle(reader=FakeReader({}), writer=writer, alerts=alerts),
    )
    async with harness.client:
        response = await harness.client.get("/v1/push/preferences", headers=HEADERS)

    assert response.status_code == 200
    assert writer.seen == ["tenant-a"]
    assert alerts.kinds == ["tenant.registration_failed"]


@pytest.mark.asyncio
async def test_registration_is_attempted_for_a_first_seen_tenant() -> None:
    writer = FakeWriter()
    harness = Harness(TenantLifecycle(reader=FakeReader({}), writer=writer))
    async with harness.client:
        response = await harness.client.get("/v1/push/preferences", headers=HEADERS)

    assert response.status_code == 200
    assert writer.seen == ["tenant-a"]


@pytest.mark.asyncio
async def test_allowlist_mode_never_registers() -> None:
    writer = FakeWriter()
    harness = Harness(
        TenantLifecycle(reader=FakeReader({}), writer=writer, registration_mode="allowlist")
    )
    async with harness.client:
        response = await harness.client.get("/v1/push/preferences", headers=HEADERS)

    assert response.status_code == 200
    assert writer.seen == []


@pytest.mark.asyncio
async def test_statistics_surface_ignores_tenant_suspension() -> None:
    """A suspended factory's platform statistics stay readable.

    The suspended-tenant guard is registered on the business routers only; the
    statistics router never consults the lifecycle, so an operator can still
    audit a factory after it has been switched off.
    """
    harness = Harness(TenantLifecycle(reader=FakeReader({"tenant-a": suspended("tenant-a")})))
    async with harness.client:
        # No platform credential: the 401 proves the request reached the
        # statistics surface's own dependency instead of the lifecycle guard
        # (which would have produced 403 for this tenant).
        response = await harness.client.get(STATISTICS_ROUTE, headers=HEADERS)

    # Rejected by the statistics surface's own bearer dependency, not by the
    # lifecycle guard: the suspension code is absent from the body.
    assert response.status_code == 403
    assert TENANT_DISABLED_DETAIL not in response.text
    assert "bearer" in response.text
