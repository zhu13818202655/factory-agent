"""``mes_call_completed`` event construction.

The event is the only MES metering record: it carries the operation id, page
count (supporting metric only), a row-count bucket, duration, and status. It
must never carry a URL, business parameter value, or credential, and its field
set must stay inside the whitelist enforced in
``tests/unit/application/test_produced_usage_events.py`` (archive payload format
``SCHEMA_VERSION`` in ``factory_agent.application.usage``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from factory_agent.application.usage import (
    UsageContext,
    mes_call_completed_event,
    row_count_bucket,
)
from factory_agent.domain import InteractionId, SessionId, TenantId, UserId
from factory_agent.ports import UsageEvent

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
CANARY = "APPKEY-SECRET-1 mock-sign mock-access-token /api/NetYf/Plan/PlanGridPageList"


def context(tenant: str = "tenant-a", user: str = "user-a") -> UsageContext:
    return UsageContext(
        tenant_id=TenantId(tenant),
        user_id=UserId(user),
        session_id=SessionId("session-1"),
        interaction_id=InteractionId("interaction-1"),
        trace_id="a" * 32,
    )


def event(**overrides: Any) -> UsageEvent:
    """Build a completed MES call event with safe defaults."""
    params: dict[str, Any] = {
        "context": context(),
        "occurred_at": NOW,
        "operation_id": "YskQuery",
        "page_count": 1,
        "row_count": 37,
        "duration_ms": 120,
        "status": "completed",
        "error_category": None,
    }
    params.update(overrides)
    return mes_call_completed_event(**params)


def test_mes_call_event_carries_only_whitelisted_fields() -> None:
    produced = event()

    assert set(produced.payload) == {
        "event_id",
        "schema_version",
        "occurred_at",
        "tenant_id",
        "user_subject_id",
        "session_id",
        "interaction_id",
        "trace_id",
        "event_type",
        "operation_id",
        "page_count",
        "row_count_bucket",
        "duration_ms",
        "status",
        "error_category",
    }
    assert produced.payload["event_type"] == "mes_call_completed"
    assert produced.payload["operation_id"] == "YskQuery"


def test_mes_call_event_buckets_rows_and_keeps_page_count_supportive() -> None:
    produced = event(page_count=3, row_count=150)

    assert produced.payload["row_count_bucket"] == row_count_bucket(150)
    assert produced.payload["page_count"] == 3
    # page_count is a supporting metric; call counts come from event rows (D6).
    page_count = produced.payload["page_count"]
    assert isinstance(page_count, int) and page_count >= 0


@pytest.mark.parametrize(
    ("status", "error_category"),
    [
        ("completed", None),
        ("failed", "upstream_invalid"),
        ("failed", "internal_error"),
    ],
)
def test_mes_call_event_keeps_status_and_error_separate(
    status: str, error_category: str | None
) -> None:
    produced = event(status=status, error_category=error_category)

    assert produced.payload["status"] == status
    assert produced.payload["error_category"] == error_category


def test_mes_call_event_never_carries_url_values_or_credentials() -> None:
    produced = event(operation_id="PlanGridPageList", error_category="签名无效")

    serialized = json.dumps(produced.payload, ensure_ascii=False, default=str)
    assert "api/" not in serialized
    assert "app_key" not in serialized.lower() or produced.payload.get("operation_id") == "YskQuery"
    assert "sign" not in json.dumps(produced.payload.get("error_category"), ensure_ascii=False)
    for token in ("APPKEY", "access_token", "Authorization", "Bearer"):
        assert token.lower() not in serialized.lower()


def test_mes_call_event_negative_values_are_clamped() -> None:
    produced = event(page_count=-1, row_count=-5, duration_ms=-3)

    assert produced.payload["page_count"] == 0
    assert produced.payload["row_count_bucket"] == "0"
    assert produced.payload["duration_ms"] == 0


def test_mes_call_event_error_category_is_truncated() -> None:
    produced = event(status="failed", error_category="x" * 500)

    category = produced.payload["error_category"]
    assert isinstance(category, str)
    assert len(category) == 64


def test_mes_call_event_ids_are_unique() -> None:
    first = event()
    second = event()

    assert first.event_id != second.event_id
