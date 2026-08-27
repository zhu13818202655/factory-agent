from __future__ import annotations

from datetime import datetime, timezone

from support.events import (
    interaction_completed,
    interaction_started,
    llm_call_completed,
)
from usage_admin.events import (
    SUPPORTED_EVENT_TYPES,
    canonical_digest,
    parse_occurred_at,
    to_interaction_fact,
    to_llm_call_fact,
    validate_event,
)


def test_supported_event_types_match_the_contract() -> None:
    assert SUPPORTED_EVENT_TYPES == {
        "interaction_started",
        "interaction_completed",
        "llm_call_completed",
        "mes_call_completed",
        "artifact_generated",
        "artifact_downloaded",
    }


def test_valid_events_validate() -> None:
    assert validate_event(interaction_started("e1")) is None
    assert validate_event(interaction_completed("e2")) is None
    assert validate_event(llm_call_completed("e3")) is None


def test_unknown_event_type_is_rejected() -> None:
    event = interaction_started("e1")
    event["event_type"] = "not_a_real_event"
    assert validate_event(event) == "unsupported event_type 'not_a_real_event'"


def test_unknown_fields_are_rejected_like_unevaluated_properties() -> None:
    event = interaction_started("e1")
    event["employee_id"] = "E-001"  # sensitive field must never pass the whitelist
    assert validate_event(event) == "unknown fields ['employee_id']"


def test_prompt_and_payroll_fields_are_not_whitelisted() -> None:
    for field in ("question", "prompt", "answer", "result_rows", "wage", "sl", "je"):
        event = llm_call_completed("e1")
        event[field] = "canary"
        assert validate_event(event) is not None, field


def test_missing_required_field_is_rejected() -> None:
    event = interaction_started("e1")
    del event["capability"]
    assert validate_event(event) == "missing fields ['capability']"


def test_wrong_type_is_rejected() -> None:
    event = interaction_started("e1")
    event["duration_ms"] = "fast"  # type: ignore[assignment]
    event = interaction_completed("e2")
    event["duration_ms"] = "fast"  # type: ignore[assignment]
    assert validate_event(event) == "field 'duration_ms' has the wrong type"


def test_digest_is_deterministic_and_order_independent() -> None:
    first = interaction_started("e1")
    second = interaction_started("e1")
    assert canonical_digest(first) == canonical_digest(second)
    assert canonical_digest(first) == canonical_digest(dict(reversed(list(first.items()))))


def test_digest_differs_when_payload_changes() -> None:
    first = interaction_started("e1")
    second = interaction_started("e1", capability="FR-005")
    assert canonical_digest(first) != canonical_digest(second)


def test_parse_occurred_at_handles_zulu_suffix() -> None:
    event = interaction_started("e1", occurred_at="2026-08-27T06:00:00Z")
    parsed = parse_occurred_at(event)
    assert parsed.isoformat().endswith("+00:00")


def test_fact_extraction_keeps_no_sensitive_identity() -> None:
    event = interaction_started("e1", user_subject_id="a" * 64)
    fact = to_interaction_fact(event, received_at=datetime.now(timezone.utc))
    assert fact.user_subject_id == "a" * 64
    assert fact.tenant_id == "tenant-a"


def test_llm_fact_extraction() -> None:
    event = llm_call_completed("e1")
    fact = to_llm_call_fact(event, received_at=datetime.now(timezone.utc))
    assert fact.prompt_tokens == 120
    assert fact.actual_model == "qwen3-32b"
