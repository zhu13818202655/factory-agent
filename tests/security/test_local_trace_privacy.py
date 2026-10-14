import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.application.trace import TraceCapability, assemble_trace
from factory_agent.application.trace_report import render_trace_report
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import DeployEnv, FactoryAgentSettings
from factory_agent.domain import (
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.execution.sandbox_runtime import InteractionSandbox
from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
    suspend_debug_capture,
)
from factory_agent.ports.sandbox import SandboxTable
from factory_agent.ports.trace import TraceDebugPayload, TraceFacts
from tests.support.payload import as_list
from tests.support.session import FrozenClock

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
CANARIES = ("APPKEY-CANARY-private", "TOKEN-CANARY-private", "employee-canary-private")


@pytest.fixture(autouse=True)
def gate() -> Iterator[None]:
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


def record() -> InteractionRecord:
    return InteractionRecord(
        interaction_id=InteractionId("local-trace-int"),
        session_id=SessionId("local-trace-session"),
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        status=InteractionStatus.COMPLETED,
        state=SessionState.ANSWERED,
        input_text="统计模拟数据",
        capability_id=None,
        clarification_rounds=0,
        last_event_sequence=1,
        error_category=None,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=1),
    )


def captures() -> TraceFacts:
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(CaptureScope("tenant-a", "user-a", "local-trace-session", "local-trace-int"))
    with InteractionSandbox(allowed_tables=("detail",)) as sandbox:
        sandbox.register_table(
            SandboxTable(
                "detail",
                ({"uid": CANARIES[2], "quantity": 3},),
                (("uid", "VARCHAR"), ("quantity", "INTEGER")),
            )
        )
        result = sandbox.execute(
            "SELECT SUM(quantity) AS total FROM detail WHERE uid IN "
            "(SELECT unnest($employee_ids)) AND 'APPKEY-CANARY-private' <> '' "
            "/* Bearer TOKEN-CANARY-private */",
            {"employee_ids": [CANARIES[2]]},
        )
        assert result == [(3,)]
    rows = drain_debug_captures()
    close_capture_scope()
    return TraceFacts(
        debug_payloads={
            item.span_key: TraceDebugPayload(
                span_key=item.span_key,
                input=item.payload.input,
                output=item.payload.output,
                truncated=item.payload.truncated,
                original_rows=item.payload.original_rows,
                original_bytes=item.payload.original_bytes,
                kind=item.kind,
                operation_id=item.operation_id,
                stage=item.stage,
                occurred_at=item.occurred_at,
            )
            for item in rows
        }
    )


def test_real_sandbox_to_report_keeps_sql_structure_without_sensitive_rows_or_bindings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    facts = captures()
    document = assemble_trace(
        record(), facts, capability=TraceCapability("dev", True, None, "full")
    )
    html = render_trace_report(document)
    spans = as_list(document["spans"])
    assert len(spans) >= 4
    serialized = json.dumps(document, ensure_ascii=False)
    assert "SUM(quantity)" in serialized
    assert "employee_ids" in serialized
    assert "output_rows" in serialized
    for canary in CANARIES:
        assert canary not in serialized
        assert canary not in html
        assert canary not in caplog.text


def test_capture_none_ignores_local_payloads_even_if_the_store_contains_them() -> None:
    facts = captures()
    document = assemble_trace(
        record(), facts, capability=TraceCapability("prod", False, "environment", "none")
    )
    assert document["spans"] == []
    assert "SUM(quantity)" not in json.dumps(document)


def test_suppressed_sandbox_never_captures_rows_or_sql() -> None:
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(CaptureScope("tenant-a", "user-a", "session-a", "interaction-a"))
    with suspend_debug_capture(), InteractionSandbox() as sandbox:
        assert sandbox.execute("SELECT 'TOKEN-CANARY-private'") == [(CANARIES[1],)]
    assert drain_debug_captures() == ()


@pytest.mark.parametrize("environment", ["cert", "staging", "prod"])
@pytest.mark.asyncio
async def test_non_developer_environment_has_no_content_route_or_active_local_capture(
    environment: DeployEnv,
) -> None:
    settings = FactoryAgentSettings(environment=environment, debug_trace_enabled=True)
    app = create_app(settings, DependencyOverrides(clock=FrozenClock(NOW)))
    assert not settings.debug_trace_active
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test.invalid",
    ) as client:
        response = await client.get("/v1/interactions/local-trace-int/trace.html")
    assert response.status_code == 404
