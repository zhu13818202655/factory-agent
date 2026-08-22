from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tests.support.usage_events import (
    FakeUsageEventIngest,
    FakeUsageEventProducer,
    FakeUsageEventRollup,
)

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "usage-events" / "v1"
SCHEMA_NAMES = ("envelope", "interaction", "llm", "mes", "artifact")


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_ROOT / f"{name}.schema.json").read_text(encoding="utf-8"))


def validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    if name != "envelope":
        schema["allOf"][0] = load_schema("envelope")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def base_event(event_type: str) -> dict[str, object]:
    return {
        "event_id": "00000000-0000-4000-8000-000000000001",
        "schema_version": "1.0",
        "occurred_at": "2026-08-21T08:00:00Z",
        "tenant_id": "tenant-demo-1",
        "user_subject_id": "a" * 64,
        "session_id": "session-1",
        "interaction_id": "interaction-1",
        "trace_id": "b" * 32,
        "event_type": event_type,
    }


def valid_events() -> list[tuple[str, dict[str, object]]]:
    interaction = base_event("interaction_completed")
    interaction.update(
        status="completed",
        duration_ms=120,
        mes_duration_ms=50,
        llm_duration_ms=60,
        local_duration_ms=10,
        result_rows_bucket="1-10",
        error_category=None,
    )
    llm = base_event("llm_call_completed")
    llm.update(
        logical_call_id="call-1",
        stage="compose",
        model_alias="answer",
        actual_model="model-a",
        attempt=1,
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=0,
        reasoning_tokens=0,
        duration_ms=60,
        status="completed",
        fallback_reason=None,
        error_category=None,
    )
    mes = base_event("mes_call_completed")
    mes.update(
        operation_id="C1",
        page_count=1,
        row_count_bucket="1-10",
        duration_ms=50,
        status="completed",
        error_category=None,
    )
    artifact = base_event("artifact_generated")
    artifact.update(format="xlsx", size_bucket="10KiB-1MiB", status="completed")
    return [("interaction", interaction), ("llm", llm), ("mes", mes), ("artifact", artifact)]


@pytest.mark.parametrize(("schema_name", "event"), valid_events())
def test_usage_event_examples_validate(schema_name: str, event: dict[str, object]) -> None:
    validator(schema_name).validate(event)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.parametrize(("schema_name", "event"), valid_events())
def test_usage_events_reject_unapproved_business_or_text_fields(
    schema_name: str, event: dict[str, object]
) -> None:
    event["question_text"] = "synthetic but forbidden"

    with pytest.raises(Exception):
        validator(schema_name).validate(event)  # pyright: ignore[reportUnknownMemberType]


def test_usage_event_fakes_copy_deduplicate_and_roll_up() -> None:
    event = valid_events()[0][1]
    producer = FakeUsageEventProducer()
    producer.emit(event)
    event["status"] = "failed"
    assert producer.events[0]["status"] == "completed"

    ingest = FakeUsageEventIngest()
    assert ingest.ingest(producer.events[0]) == "accepted"
    assert ingest.ingest(producer.events[0]) == "duplicate"
    conflict = deepcopy(producer.events[0])
    conflict["status"] = "failed"
    assert ingest.ingest(conflict) == "rejected"

    rollup = FakeUsageEventRollup()
    rollup.apply(ingest.events)
    assert rollup.counts == {"interaction_completed": 1}
