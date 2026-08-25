from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.usage import (
    SCHEMA_VERSION,
    UsageContext,
    completion_status,
    interaction_completed_event,
    interaction_started_event,
    llm_call_event,
    new_trace_id,
    pseudonymous_subject,
    row_count_bucket,
)
from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionStatus,
    Role,
    SessionId,
    TenantId,
    UserId,
)
from factory_agent.ports import ModelStage

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
CANARY_TEXT = "员工 E-CANARY 上月工资 8213.44 元"


def context(tenant: str = "tenant-a", user: str = "user-a") -> UsageContext:
    return UsageContext(
        tenant_id=TenantId(tenant),
        user_id=UserId(user),
        session_id=SessionId("session-1"),
        interaction_id=InteractionId("interaction-1"),
        trace_id="a" * 32,
    )


def test_subject_is_pseudonymous_stable_and_tenant_scoped() -> None:
    same = pseudonymous_subject(TenantId("tenant-a"), UserId("user-a"))
    other_tenant = pseudonymous_subject(TenantId("tenant-b"), UserId("user-a"))

    assert same == pseudonymous_subject(TenantId("tenant-a"), UserId("user-a"))
    assert same != other_tenant
    assert "user-a" not in same
    assert 32 <= len(same) <= 128


@pytest.mark.parametrize(
    ("rows", "bucket"),
    [
        (0, "0"),
        (1, "1-10"),
        (10, "1-10"),
        (11, "11-100"),
        (100, "11-100"),
        (101, "101-1000"),
        (1000, "101-1000"),
        (1001, "1001+"),
        (99999, "1001+"),
    ],
)
def test_row_counts_are_bucketed(rows: int, bucket: str) -> None:
    assert row_count_bucket(rows) == bucket


def test_started_event_carries_only_whitelisted_fields() -> None:
    event = interaction_started_event(
        context(),
        occurred_at=NOW,
        capability=CapabilityId("FR-001"),
        entrypoint="api",
        role=Role.EMPLOYEE,
    )

    assert set(event.payload) == {
        "event_id",
        "schema_version",
        "occurred_at",
        "tenant_id",
        "user_subject_id",
        "session_id",
        "interaction_id",
        "trace_id",
        "event_type",
        "capability",
        "entrypoint",
        "role_category",
    }
    assert event.payload["schema_version"] == SCHEMA_VERSION


def test_llm_event_never_carries_prompt_or_completion_text() -> None:
    event = llm_call_event(
        context(),
        occurred_at=NOW,
        logical_call_id="call-1",
        stage=ModelStage.EXTRACT,
        model_alias="factory-fast",
        actual_model="qwen3-32b-local",
        attempt=2,
        prompt_tokens=42,
        completion_tokens=9,
        duration_ms=120,
    )

    assert "prompt" not in event.payload
    assert "messages" not in event.payload
    assert "content" not in event.payload
    assert CANARY_TEXT not in str(event.payload)
    assert event.payload["attempt"] == 2


def test_completed_event_never_carries_detail_rows_or_scope_ids() -> None:
    event = interaction_completed_event(
        context(),
        occurred_at=NOW,
        status="completed",
        duration_ms=900,
        mes_duration_ms=400,
        llm_duration_ms=300,
        local_duration_ms=20,
        result_row_count=37,
        error_category=None,
    )

    assert event.payload["result_rows_bucket"] == "11-100"
    assert "rows" not in event.payload
    assert "employee_ids" not in event.payload
    assert "dept_ids" not in event.payload


def test_negative_durations_and_attempts_are_clamped() -> None:
    event = llm_call_event(
        context(),
        occurred_at=NOW,
        logical_call_id="call-1",
        stage=ModelStage.CLARIFY,
        model_alias="factory-fast",
        actual_model="m",
        attempt=0,
        duration_ms=-5,
        prompt_tokens=-1,
    )

    assert event.payload["attempt"] == 1
    assert event.payload["duration_ms"] == 0
    assert event.payload["prompt_tokens"] == 0


def test_free_text_categories_are_truncated() -> None:
    event = interaction_completed_event(
        context(),
        occurred_at=NOW,
        status="failed",
        duration_ms=1,
        mes_duration_ms=0,
        llm_duration_ms=0,
        local_duration_ms=0,
        result_row_count=0,
        error_category="x" * 500,
    )

    category = event.payload["error_category"]
    assert isinstance(category, str)
    assert len(category) == 64


def test_event_ids_are_unique_per_event() -> None:
    shared = context()
    first = interaction_started_event(
        shared, occurred_at=NOW, capability=None, entrypoint="api", role=Role.OWNER
    )
    second = interaction_started_event(
        shared, occurred_at=NOW, capability=None, entrypoint="api", role=Role.OWNER
    )

    assert first.event_id != second.event_id


def test_trace_ids_are_32_hex_characters() -> None:
    trace_id = new_trace_id()

    assert len(trace_id) == 32
    assert all(char in "0123456789abcdef" for char in trace_id)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (InteractionStatus.COMPLETED, "completed"),
        (InteractionStatus.FAILED, "failed"),
        (InteractionStatus.CANCELLED, "cancelled"),
        (InteractionStatus.PENDING, "failed"),
    ],
)
def test_interaction_status_maps_to_the_contract_vocabulary(
    status: InteractionStatus, expected: str
) -> None:
    assert completion_status(status) == expected
