"""Trace routes: what is mounted where, and who may read it.

The plan's security argument rests on two asymmetries that only exist at the
HTTP layer, so they are asserted here rather than in the read-model tests:

* the content report is **absent** outside a developer environment, not
  present-and-refusing — so "misconfigured" and "not deployed" answer the same
  way and an attacker gains no probe;
* a foreign interaction and a missing one are the same 404, so the response
  cannot be used to enumerate another tenant's interaction ids.

The audit gate is the third: bytes are released only after the audit record has
landed (DEC-014), which is why the failing-sink case must withhold the report
rather than merely log about it.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.trace import TraceCapability, TraceService
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    Role,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditSink,
    AuditWriteError,
    InMemoryAuditSink,
)
from factory_agent.observability.debug_trace import close_capture_scope, configure_debug_trace
from factory_agent.ports.session import InteractionOwner
from factory_agent.ports.trace import InteractionFactRecord, MesSpanRecord, TraceFacts
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.session import FrozenClock

NOW = datetime(2026, 9, 22, 0, 20, 31, tzinfo=timezone.utc)
HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"}

FULL = TraceCapability(
    environment="local", report_available=True, reason=None, content_capture="full"
)


@pytest.fixture(autouse=True)
def _close_capture_gate() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """``create_app`` configures the process-wide capture gate from settings.

    Left open, a case that builds a developer-environment app would hand capture
    to every later test in the session. The gate is process state, so it is
    reset here exactly as the capture tests reset it.
    """
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


def record() -> InteractionRecord:
    return InteractionRecord(
        interaction_id=InteractionId("int-1"),
        session_id=SessionId("session-1"),
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        status=InteractionStatus.COMPLETED,
        state=SessionState.ANSWERED,
        input_text="上个月计件工资是多少",
        capability_id=CapabilityId("FR-003"),
        clarification_rounds=0,
        last_event_sequence=9,
        error_category=None,
        created_at=NOW,
        updated_at=NOW + timedelta(milliseconds=4_008),
        completed_at=NOW + timedelta(milliseconds=4_008),
    )


class _Interactions:
    """Ownership-filtered lookup: ``None`` is both "missing" and "not yours"."""

    def __init__(self, found: InteractionRecord | None) -> None:
        self._found = found

    async def get_interaction(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord | None:
        return self._found


class _Facts:
    def __init__(self, facts: TraceFacts) -> None:
        self._facts = facts

    async def load(self, *, tenant_id: str, user_id: str, interaction_id: str) -> TraceFacts:
        return self._facts


class _FailingAuditSink:
    async def record(self, event: AuditEvent) -> None:
        raise AuditWriteError("audit sink unavailable")


def _facts() -> TraceFacts:
    return TraceFacts(
        mes_spans=(
            MesSpanRecord(
                operation_id="SystemToken",
                page_count=1,
                row_count_bucket="1-10",
                duration_ms=12,
                status="completed",
                error_category=None,
                started_at=NOW,
                ended_at=NOW + timedelta(milliseconds=12),
                occurred_at=NOW + timedelta(milliseconds=12),
            ),
        ),
        interaction_fact=InteractionFactRecord(
            status="completed",
            duration_ms=4_008,
            mes_duration_ms=12,
            llm_duration_ms=0,
            local_duration_ms=3_996,
        ),
    )


def _client(
    settings: FactoryAgentSettings,
    *,
    found: InteractionRecord | None = record(),
    capability: TraceCapability = FULL,
    audit: AuditSink | None = None,
    service: TraceService | None = None,
) -> httpx.AsyncClient:
    overrides = DependencyOverrides(
        clock=FrozenClock(NOW),
        authorization=AuthorizationService(
            memberships=FakeMembershipSource(
                memberships_by_credential={
                    ("tenant-a", "user-a"): membership("user-a", "tenant-a", "emp-1", Role.EMPLOYEE)
                }
            ),
            organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
            versions=FixedScopeVersionAssigner(),
        ),
        audit=audit if audit is not None else InMemoryAuditSink(),
        trace=service
        if service is not None
        else TraceService(_Interactions(found), _Facts(_facts()), capability),  # type: ignore[arg-type]
    )
    app = create_app(settings, overrides)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


async def _get(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.get(path, headers=HEADERS)


@pytest.mark.asyncio
async def test_capabilities_answer_200_in_production_with_the_report_unavailable() -> None:
    """The answer is a fact, not a status code: 200 with ``available: false``.

    A status-coded refusal would make "this deployment cannot" and "this
    deployment is broken" the same signal to the app client.
    """
    async with _client(FactoryAgentSettings(environment="prod")) as http:
        response = await _get(http, "/v1/trace/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "environment": "prod",
        "report_available": False,
        "content_capture": "none",
        "reason": "environment",
    }


@pytest.mark.asyncio
async def test_capabilities_need_no_store_at_all() -> None:
    """Capability discovery is a settings read, so it survives a bare deployment."""
    settings = FactoryAgentSettings(environment="prod")
    app = create_app(settings, DependencyOverrides(clock=FrozenClock(NOW)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        capabilities = await _get(http, "/v1/trace/capabilities")
        timeline = await _get(http, "/v1/interactions/int-1/trace")

    assert capabilities.status_code == 200
    # The timeline itself *is* a database read, so it refuses rather than
    # inventing an empty trace.
    assert timeline.status_code == 503


@pytest.mark.asyncio
async def test_a_developer_environment_with_capture_on_declares_full_content() -> None:
    settings = FactoryAgentSettings(environment="local", debug_trace_enabled=True)

    async with _client(
        settings,
        capability=TraceCapability(
            environment="local", report_available=True, reason=None, content_capture="full"
        ),
    ) as http:
        response = await _get(http, "/v1/trace/capabilities")

    assert response.json()["report_available"] is True
    assert response.json()["content_capture"] == "full"
    assert response.json()["reason"] is None


@pytest.mark.asyncio
async def test_the_switch_alone_does_not_enable_the_report_in_production() -> None:
    """A stray environment variable must not widen the boundary."""
    async with _client(FactoryAgentSettings(environment="prod", debug_trace_enabled=True)) as http:
        response = await _get(http, "/v1/trace/capabilities")
        report = await _get(http, "/v1/interactions/int-1/trace.html")

    assert response.json()["report_available"] is False
    assert response.json()["reason"] == "environment"
    assert report.status_code == 404


@pytest.mark.asyncio
async def test_the_report_route_is_absent_outside_a_developer_environment() -> None:
    """Same document, one route mounted: the 404 is the mount, not a refusal."""
    async with _client(FactoryAgentSettings(environment="prod")) as http:
        timeline = await _get(http, "/v1/interactions/int-1/trace")
        report = await _get(http, "/v1/interactions/int-1/trace.html")

    # The A-level timeline answers with the very document the report route
    # would have rendered, so the report's 404 cannot be blamed on the service.
    assert timeline.status_code == 200
    assert timeline.json()["interaction_id"] == "int-1"
    assert report.status_code == 404


@pytest.mark.asyncio
async def test_the_report_route_is_absent_when_capture_is_switched_off() -> None:
    """Developer environment, switch off: no content route either."""
    settings = FactoryAgentSettings(environment="local", debug_trace_enabled=False)

    async with _client(settings) as http:
        report = await _get(http, "/v1/interactions/int-1/trace.html")

    assert report.status_code == 404


@pytest.mark.asyncio
async def test_a_foreign_interaction_is_the_same_404_on_both_routes() -> None:
    """Indistinguishable by construction: identical status and identical body."""
    settings = FactoryAgentSettings(environment="local", debug_trace_enabled=True)
    audit = InMemoryAuditSink()

    async with _client(settings, found=None, audit=audit) as http:
        timeline = await _get(http, "/v1/interactions/other-tenant-int/trace")
        report = await _get(http, "/v1/interactions/other-tenant-int/trace.html")

    assert timeline.status_code == report.status_code == 404
    assert timeline.json() == report.json()
    # Nothing was released, so nothing was audited as released.
    assert audit.events == []


@pytest.mark.asyncio
async def test_the_report_is_released_only_after_the_audit_lands() -> None:
    settings = FactoryAgentSettings(environment="local", debug_trace_enabled=True)
    audit = InMemoryAuditSink()

    async with _client(settings, audit=audit) as http:
        response = await _get(http, "/v1/interactions/int-1/trace.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The report is data, not markup to be sniffed into something else.
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert 'attachment; filename="trace.html"' in disposition
    # A browser prefers the RFC 5987 form and therefore renders the Chinese
    # name; the ASCII fallback is what an App client reads.
    assert f"filename*=UTF-8''{quote('链路追踪报告-int-1.html')}" in disposition
    # The body is the real document, not an empty shell.
    assert "int-1" in response.text

    (event,) = audit.events
    assert event.event_type is AuditEventType.DOWNLOAD
    assert event.outcome.value == "allowed"
    assert event.tenant_id == "tenant-a"
    assert event.request_id == "int-1"
    assert event.employee_count == 1
    assert event.dept_count == 1
    assert event.scope_fingerprint is not None
    assert "emp-1" not in event.scope_fingerprint


@pytest.mark.asyncio
async def test_the_report_is_withheld_when_the_audit_sink_fails() -> None:
    """Fail closed: no audit record means no bytes, not a warning and a release."""
    settings = FactoryAgentSettings(environment="local", debug_trace_enabled=True)

    async with _client(settings, audit=_FailingAuditSink()) as http:
        response = await _get(http, "/v1/interactions/int-1/trace.html")

    assert response.status_code == 503
    assert "<!DOCTYPE html>" not in response.text
    assert "int-1" not in response.text


@pytest.mark.asyncio
async def test_the_timeline_reports_a_missing_service_rather_than_an_empty_trace() -> None:
    """``None`` is "no store configured", and it must not read as "no activity"."""
    settings = FactoryAgentSettings(environment="local", debug_trace_enabled=True)
    app = create_app(settings, DependencyOverrides(clock=FrozenClock(NOW)))
    # A local deployment without ``postgres_url`` builds no trace service.
    assert app.state.container.trace is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        response = await _get(http, "/v1/interactions/int-1/trace")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_the_timeline_carries_the_request_id_of_the_read() -> None:
    """The report header prints it, so it must come from the request, not a store."""
    async with _client(FactoryAgentSettings(environment="local", debug_trace_enabled=True)) as http:
        response = await http.get(
            "/v1/interactions/int-1/trace",
            headers={**HEADERS, "X-Request-Id": "req-9f3a"},
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == "req-9f3a"
