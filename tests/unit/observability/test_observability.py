"""Story 2: redaction, audit baseline, request context, and logging tests."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from factory_agent.config import FactoryAgentSettings
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    InMemoryAuditSink,
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
