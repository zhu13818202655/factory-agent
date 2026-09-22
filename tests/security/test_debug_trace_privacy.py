"""Debug-trace privacy: the B channel's departure from §Forbidden Log Content.

ADR-0004 forbids business content in the log stream, and the B channel is the
approved exception: it keeps business values and withholds credential material
instead. That inversion only stays safe while three things hold, and each is
asserted here rather than left to a code review:

1. the exception is **narrow** — credential-shaped keys and credential-shaped
   free text never reach the store or the report;
2. it is **bounded** — the capability is closed until an operator opens it, and
   a caller outside an interaction scope captures nothing;
3. it is **unreachable in production** — the content route does not exist
   outside a developer environment, whatever the switch says.

These run the real capture, assembly and rendering path with credential canaries
rather than inspecting the redaction function in isolation, so a canary that
survives any one stage fails the suite.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.trace import TraceCapability, assemble_trace
from factory_agent.application.trace_report import render_trace_report
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    debug_capture_enabled,
    drain_debug_captures,
    llm_span_key,
    mes_span_key,
    open_capture_scope,
    record_debug_capture,
)
from factory_agent.ports.trace import (
    LlmSpanRecord,
    MesSpanRecord,
    TraceDebugPayload,
    TraceFacts,
)
from tests.support.session import FrozenClock

NOW = datetime(2026, 9, 22, 0, 20, 31, tzinfo=timezone.utc)

CANARY_APP_KEY = "APPKEY-SECRET-9f3a"
CANARY_SIGN = "sign-9f3a2c88deadbeef"
CANARY_TOKEN = "access-token-9f3a2c88"
CANARIES = (CANARY_APP_KEY, CANARY_SIGN, CANARY_TOKEN)

#: Business content the channel exists to keep. Separated from the canaries so
#: one rule cannot satisfy both assertions.
BUSINESS = ("上月工资是多少", "模拟员工-张三", "8213.44")

SCOPE = CaptureScope(
    tenant_id="tenant-a",
    user_id="user-a",
    session_id="session-1",
    interaction_id="int-1",
)

FULL = TraceCapability(
    environment="local", report_available=True, reason=None, content_capture="full"
)


@pytest.fixture(autouse=True)
def _closed_gate() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """The gate is process state; leaving it open would leak capture forward."""
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


def _record() -> InteractionRecord:
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


LLM_KEY = llm_span_key("lc_8f21")
MES_KEY = mes_span_key("SystemToken", NOW)


def _capture() -> dict[str, TraceDebugPayload]:
    """Run the real capture path for both channels and store what it kept.

    Both channels are exercised because they carry different business text: the
    LLM span holds the question and the MES span holds the rows. A canary that
    survives only one of them would still be a leak.
    """
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(SCOPE)
    record_debug_capture(
        span_key=LLM_KEY,
        kind="llm",
        input_payload={
            "prompt": BUSINESS[0],
            "authorization": f"Bearer {CANARY_TOKEN}",
        },
        output_payload={"text": '{"intent": "payroll_query"}'},
        occurred_at=NOW,
        stage="extract",
        logical_call_id="lc_8f21",
        attempt=1,
    )
    record_debug_capture(
        span_key=MES_KEY,
        kind="mes",
        # A signed URL puts the secret in the text, not only in a named field.
        input_payload={
            "url": f"https://mes.invalid/api/x?app_key={CANARY_APP_KEY}&sign={CANARY_SIGN}",
            "app_key": CANARY_APP_KEY,
            "Cookie": "session=abc",
            "dh": "SO-2026-0001",
        },
        output_payload={"rows": [{"name": BUSINESS[1], "amount": BUSINESS[2]}]},
        occurred_at=NOW,
        operation_id="SystemToken",
    )
    drained = drain_debug_captures()
    close_capture_scope()
    return {
        captured.span_key: TraceDebugPayload(
            span_key=captured.span_key,
            input=captured.payload.input,
            output=captured.payload.output,
            truncated=captured.payload.truncated,
            original_rows=captured.payload.original_rows,
            original_bytes=captured.payload.original_bytes,
        )
        for captured in drained
    }


def _facts(payloads: dict[str, TraceDebugPayload]) -> TraceFacts:
    """Attach the captures to the spans they belong to.

    A payload with no matching span never reaches the document, so the spans are
    part of the fixture rather than decoration: without them these tests would
    pass vacuously.
    """
    return TraceFacts(
        llm_spans=(
            LlmSpanRecord(
                logical_call_id="lc_8f21",
                stage="extract",
                model_alias="factory-fast",
                actual_model="Qwen3-32B-Instruct",
                attempt=1,
                prompt_tokens=1_842,
                completion_tokens=96,
                cached_tokens=0,
                reasoning_tokens=0,
                duration_ms=1_180,
                status="completed",
                started_at=NOW,
                ended_at=NOW + timedelta(milliseconds=1_180),
                occurred_at=NOW + timedelta(milliseconds=1_180),
            ),
        ),
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
        debug_payloads=payloads,
    )


def test_the_captured_payload_keeps_business_content_and_withholds_credentials() -> None:
    payloads = _capture()

    serialized = json.dumps(
        [
            {"span_key": key, "input": item.input, "output": item.output}
            for key, item in payloads.items()
        ],
        ensure_ascii=False,
        default=str,
    )
    for canary in CANARIES:
        assert canary not in serialized
    assert "session=abc" not in serialized
    # Business content is retained: withholding it would make the channel
    # useless, which is the trade the ADR exception documents.
    for value in (*BUSINESS, "SO-2026-0001"):
        assert value in serialized


def test_neither_the_document_nor_the_report_carries_a_credential_canary() -> None:
    """The canary must not survive into the wire format or the rendered page."""
    document = assemble_trace(_record(), _facts(_capture()), capability=FULL)
    html = render_trace_report(document)

    serialized = json.dumps(document, ensure_ascii=False, default=str)
    for canary in CANARIES:
        assert canary not in serialized
        assert canary not in html
    # The content that makes the channel worth having is present in both.
    assert BUSINESS[2] in serialized
    assert BUSINESS[2] in html


def test_capture_is_closed_until_an_operator_opens_it() -> None:
    """The default is "no capability", not "a switch that happens to be off"."""
    assert debug_capture_enabled() is False

    open_capture_scope(SCOPE)
    record_debug_capture(
        span_key="mes:SystemToken:2026-09-22T00:20:31+00:00",
        kind="mes",
        input_payload={"prompt": BUSINESS[0]},
        output_payload=None,
        occurred_at=NOW,
    )

    assert drain_debug_captures() == ()


def test_a_caller_outside_an_interaction_scope_captures_nothing() -> None:
    """A health probe reaches the same adapter, and must write no content."""
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)

    record_debug_capture(
        span_key="mes:SystemToken:2026-09-22T00:20:31+00:00",
        kind="mes",
        input_payload={"prompt": BUSINESS[0]},
        output_payload=None,
        occurred_at=NOW,
    )

    assert drain_debug_captures() == ()


@pytest.mark.asyncio
async def test_the_content_report_does_not_exist_in_a_production_deployment() -> None:
    """The switch is a preference; the environment is the boundary (ADR-0004)."""
    settings = FactoryAgentSettings(environment="prod", debug_trace_enabled=True)
    app = create_app(settings, DependencyOverrides(clock=FrozenClock(NOW)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        capabilities = await http.get(
            "/v1/trace/capabilities",
            headers={TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"},
        )
        report = await http.get(
            "/v1/interactions/int-1/trace.html",
            headers={TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"},
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["report_available"] is False
    assert capabilities.json()["reason"] == "environment"
    # Not registered at all: a production probe learns nothing about the route.
    assert report.status_code == 404
