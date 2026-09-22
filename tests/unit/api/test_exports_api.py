"""Download gate: audited release, fail-closed when the audit sink fails."""

from datetime import datetime, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import CapabilityId, Role
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditSink,
    AuditWriteError,
    InMemoryAuditSink,
)
from factory_agent.ports.artifacts import ArtifactExporter, ExportContent, ExportOutcome
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.session import FrozenClock

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"}
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _StubExporter:
    """Serves one transient artifact, or nothing when it is gone."""

    def __init__(self, content: bytes | None) -> None:
        self._content = content

    async def export(
        self,
        *,
        owner: InteractionOwner,
        interaction_id: str,
        capability_id: CapabilityId,
        role: str,
        function: str,
        time_range_label: str,
        result: CapabilityRunResult,
    ) -> ExportOutcome:
        raise NotImplementedError

    async def fetch(self, owner: InteractionOwner, artifact_id: str) -> ExportContent | None:
        if self._content is None:
            return None
        return ExportContent(
            artifact_id=artifact_id,
            filename="产量.xlsx",
            content_type=_XLSX_MEDIA_TYPE,
            content=self._content,
        )


class _FailingAuditSink:
    """Every audit write fails; the gate must withhold the bytes."""

    async def record(self, event: AuditEvent) -> None:
        raise AuditWriteError("audit sink unavailable")


def _client(exporter: ArtifactExporter, audit: AuditSink) -> httpx.AsyncClient:
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
        artifact_exporter=exporter,
        audit=audit,
    )
    app = create_app(FactoryAgentSettings(environment="local"), overrides)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


@pytest.mark.asyncio
async def test_release_is_audited_with_the_effective_scope_digest() -> None:
    audit = InMemoryAuditSink()

    async with _client(_StubExporter(b"xlsx-bytes"), audit) as http:
        response = await http.get("/v1/artifacts/artifact-1/download", headers=HEADERS)

    assert response.status_code == 200
    assert response.content == b"xlsx-bytes"
    (event,) = audit.events
    assert event.event_type is AuditEventType.DOWNLOAD
    assert event.outcome.value == "allowed"
    assert event.tenant_id == "tenant-a"
    assert event.request_id == "artifact-1"
    assert event.employee_count == 1
    assert event.dept_count == 1
    assert event.whole_tenant is False
    # The digest is irreversible: no raw scope id survives in it.
    assert event.scope_fingerprint is not None
    assert "emp-1" not in event.scope_fingerprint


@pytest.mark.asyncio
async def test_release_is_withheld_when_the_audit_sink_fails() -> None:
    async with _client(_StubExporter(b"xlsx-bytes"), _FailingAuditSink()) as http:
        response = await http.get("/v1/artifacts/artifact-1/download", headers=HEADERS)

    assert response.status_code == 503
    assert b"xlsx-bytes" not in response.content


@pytest.mark.asyncio
async def test_unavailable_artifact_is_a_plain_404_without_an_audit_event() -> None:
    audit = InMemoryAuditSink()

    async with _client(_StubExporter(None), audit) as http:
        response = await http.get("/v1/artifacts/artifact-1/download", headers=HEADERS)

    assert response.status_code == 404
    assert audit.events == []
