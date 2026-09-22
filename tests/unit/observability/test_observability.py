"""Redaction, audit baseline, request context, and logging tests."""

import logging
from datetime import datetime, timezone

import pytest

from factory_agent.config import DeployEnv, FactoryAgentSettings
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    InMemoryAuditSink,
    StructuredLogAuditSink,
    scope_fingerprint,
)
from factory_agent.observability.context import (
    accept_request_id,
    bind_request_id,
    current_log_context,
)
from factory_agent.observability.logging_adapter import configure_logging, get_logger
from factory_agent.observability.redaction import REDACTED, is_sensitive_key, redact_mapping

# Synthetic canary values; never real credentials or personal data.
CANARIES = (
    "postgres://app:canary-password@db:5432/app",
    "Bearer canary-token-value",
)


def test_redaction_covers_names_numbers_payroll_and_credentials() -> None:
    payload = {
        "display_name": "Synthetic Person",
        "employee_number": "SYN-001",
        "gross_amount": "1234.56",
        "unit_rate": "10.00",
        "completed_quantity": "99",
        "employee_ids": ["employee-a1"],
        "dept_ids": ["group-a1"],
        "postgres_url": CANARIES[0],
        "authorization": CANARIES[1],
        "tenant_id": "tenant-a",
        "status": "ok",
    }

    redacted = redact_mapping(payload)

    for key in (
        "display_name",
        "employee_number",
        "gross_amount",
        "unit_rate",
        "completed_quantity",
        "employee_ids",
        "dept_ids",
        "postgres_url",
        "authorization",
    ):
        assert redacted[key] == REDACTED
    assert redacted["tenant_id"] == "tenant-a"
    assert redacted["status"] == "ok"


def test_canary_values_never_survive_text_redaction() -> None:
    text = f"failed to reach {CANARIES[0]} using {CANARIES[1]}"

    from factory_agent.observability.redaction import redact_text

    sanitized = redact_text(text)

    for canary in CANARIES:
        assert canary not in sanitized


def test_is_sensitive_key_matches_case_insensitively() -> None:
    assert is_sensitive_key("Authorization")
    assert is_sensitive_key("x-api-key")
    assert not is_sensitive_key("tenant_id")
    assert not is_sensitive_key("status")


def test_scope_fingerprint_is_irreversible_and_stable() -> None:
    first = scope_fingerprint("tenant-a", ("e2", "e1"), ("g1",))
    second = scope_fingerprint("tenant-a", ("e1", "e2"), ("g1",))

    assert first == second
    assert "e1" not in first and "g1" not in first


def test_audit_event_payload_is_whitelisted() -> None:
    event = AuditEvent(
        event_type=AuditEventType.QUERY,
        outcome=AuditOutcome.DENIED,
        capability_id="FR-009",
        intent_summary="factory overview",
        scope_fingerprint=scope_fingerprint("tenant-a", None, None),
        employee_count=None,
        dept_count=None,
        whole_tenant=True,
        tenant_id="tenant-a",
        status="denied",
        occurred_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        request_id="req-1",
    )

    payload = event.to_payload()

    assert set(payload) <= {
        "event_type",
        "outcome",
        "capability_id",
        "intent_summary",
        "scope_fingerprint",
        "employee_count",
        "dept_count",
        "whole_tenant",
        "tenant_id",
        "status",
        "occurred_at",
        "request_id",
    }
    serialized = repr(payload)
    for leak in ("employee-a1", "group-a1", *CANARIES):
        assert leak not in serialized


@pytest.mark.asyncio
async def test_in_memory_audit_sink_records_denials() -> None:
    sink = InMemoryAuditSink()
    event = AuditEvent(
        event_type=AuditEventType.API_CALL,
        outcome=AuditOutcome.DENIED,
        capability_id=None,
        intent_summary=None,
        scope_fingerprint=None,
        employee_count=None,
        dept_count=None,
        whole_tenant=False,
        tenant_id=None,
        status="denied",
        occurred_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        request_id="req-2",
    )

    await sink.record(event)

    assert sink.events == [event]


@pytest.mark.asyncio
async def test_structured_log_audit_sink_emits_a_redacted_correlated_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(FactoryAgentSettings(environment="prod", log_format="json"))
    bind_request_id("req-audit")
    fingerprint = scope_fingerprint("tenant-a", ("employee-a1",), ("group-a1",))
    sink = StructuredLogAuditSink()

    await sink.record(
        AuditEvent(
            event_type=AuditEventType.DOWNLOAD,
            outcome=AuditOutcome.ALLOWED,
            capability_id=None,
            intent_summary=None,
            scope_fingerprint=fingerprint,
            employee_count=1,
            dept_count=1,
            whole_tenant=False,
            tenant_id="tenant-a",
            status="allowed",
            occurred_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
            request_id="artifact-a1",
        )
    )

    output = capsys.readouterr().out

    assert "audit.download" in output
    assert "'environment': 'prod'" in output
    assert fingerprint in output
    # The event is namespaced, so its own request_id cannot shadow the inbound
    # request correlation field on the same record.
    assert "'request_id': 'req-audit'" in output
    assert "'request_id': 'artifact-a1'" in output
    for leak in ("employee-a1", "group-a1"):
        assert leak not in output


def test_request_id_header_validation() -> None:
    assert accept_request_id("abc-123") == "abc-123"
    generated = accept_request_id("../etc/passwd with spaces")
    assert generated != "../etc/passwd with spaces"
    assert len(generated) == 32
    assert accept_request_id(None) != ""
    assert len(accept_request_id("x" * 500)) == 32


def test_log_context_binds_request_tenant_interaction() -> None:
    bind_request_id("req-42")

    context = current_log_context()

    assert context["request_id"] == "req-42"


def test_structured_logging_intercepts_standard_logging_without_canaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = FactoryAgentSettings(log_format="json", log_level="INFO")
    configure_logging(settings)
    logger = get_logger("test_component")

    logger.warning("operation_failed", operation_id="C1_listPieceworkRecords", status="failed")
    logging.getLogger("uvicorn.error").warning("standard record forwarded")

    output = capsys.readouterr().out
    assert "operation_failed" in output
    assert "standard record forwarded" in output
    for canary in CANARIES:
        assert canary not in output


# --- Noise reduction: transport narration and access-log filtering ------------


def _json_settings(
    *,
    environment: DeployEnv = "prod",
    log_level: str = "INFO",
    log_third_party_level: str = "WARNING",
    debug_trace_enabled: bool = False,
) -> FactoryAgentSettings:
    """Build JSON-format settings for the logger tests.

    Spelled out rather than assembled from ``**overrides``: a ``dict[str, object]``
    spread collapses every field to ``object`` at the call site, which hides a
    mistyped environment tier behind an unreadable pile of diagnostics.
    """
    return FactoryAgentSettings(
        environment=environment,
        log_format="json",
        log_level=log_level,
        log_third_party_level=log_third_party_level,
        debug_trace_enabled=debug_trace_enabled,
    )


def test_transport_request_narration_is_not_forwarded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """httpx logs one INFO line per outbound request, carrying the full URL.

    That fact is already in ``mes_call_fact`` / ``llm_call_fact`` in redacted,
    structured form, while the URL includes its query string — which ADR-0004
    forbids. Warnings from the same library still land, so a transport failure
    stays diagnosable.
    """
    configure_logging(_json_settings())
    httpx_logger = logging.getLogger("httpx")

    httpx_logger.info(
        'HTTP Request: GET %s "%d %s"', "http://mes.example/query?order=SO-1", 200, "OK"
    )
    httpx_logger.warning("connection reset by peer")

    output = capsys.readouterr().out
    assert "connection reset by peer" in output
    assert "HTTP Request" not in output
    assert "SO-1" not in output


def test_access_log_drops_probes_and_successes_but_keeps_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """uvicorn logs every request at INFO whatever the status code.

    A level pin would therefore silence the 5xx that justify keeping an access
    log at all, so the volume is removed by filtering instead: probe traffic is
    infrastructure rather than a user request, and a successful request is
    already recorded by the application.
    """
    configure_logging(_json_settings())
    access = logging.getLogger("uvicorn.access")

    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/health", "1.1", 200)
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/v1/conversations", "1.1", 200)
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:1",
        "POST",
        "/v1/sessions/s1/interactions",
        "1.1",
        500,
    )

    output = capsys.readouterr().out
    assert "/health" not in output
    assert "/v1/conversations" not in output
    assert "500" in output


def test_forwarded_records_carry_their_origin(capsys: pytest.CaptureFixture[str]) -> None:
    """Without the origin a forwarded record looks like an application one.

    ``depth`` only recovers the caller frame when the stack happens to line up,
    and the library's own name and line number were dropped entirely — which is
    what made the forwarded chatter impossible to attribute.
    """
    configure_logging(_json_settings())

    logging.getLogger("uvicorn.error").warning("application startup complete")

    output = capsys.readouterr().out
    assert "'origin': 'uvicorn.error:" in output


def test_url_query_strings_are_redacted_wherever_they_appear(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ADR-0004 forbids MES URLs with query parameters, on every log path.

    The application path matters as much as the transport one: a URL
    interpolated into an event is already rendered by the time the sink runs, so
    the key-based policy cannot see it.
    """
    configure_logging(_json_settings())

    get_logger("test_component").warning(
        "mes.call.failed url=http://mes.example/api/query?order=SO-1&emp=SYN-001"
    )

    output = capsys.readouterr().out
    assert "SO-1" not in output
    assert "SYN-001" not in output
    assert "http://mes.example/api/query" in output
    assert "[REDACTED]" in output


# --- Environment tier reporting at startup -----------------------------------


def test_startup_states_the_resolved_environment_tier(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One line instead of a guess about which configuration is in force."""
    configure_logging(_json_settings(environment="local", debug_trace_enabled=True))

    output = capsys.readouterr().out
    assert "config.environment_effective" in output
    assert "content_capture=full" in output
    assert "debug_trace=on" in output


def test_debug_trace_on_a_restricted_deployment_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused override must not look as if it had taken effect."""
    configure_logging(_json_settings(environment="prod", debug_trace_enabled=True))

    output = capsys.readouterr().out
    assert "config.debug_trace_refused" in output
    assert "requested=true applied=false" in output
    assert "content_capture=none" in output


def test_debug_records_are_withheld_in_production(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_json_settings(environment="prod", log_level="DEBUG"))

    get_logger("test_component").debug("local_only_diagnostic")

    assert "local_only_diagnostic" not in capsys.readouterr().out


def test_unknown_third_party_log_level_is_a_startup_error() -> None:
    """Substituting a default would hide a typo in a log-volume knob."""
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging(_json_settings(log_third_party_level="CHATTY"))
