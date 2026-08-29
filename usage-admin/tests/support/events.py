"""Test helpers for building valid v1 usage events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"


def _envelope(
    event_id: str,
    event_type: str,
    *,
    tenant_id: str = "tenant-a",
    user_subject_id: str | None = None,
    session_id: str = "session-1",
    interaction_id: str = "interaction-1",
    trace_id: str = "0" * 32,
    occurred_at: datetime | str | None = None,
) -> dict[str, object]:
    if user_subject_id is None:
        user_subject_id = "a" * 64
    if isinstance(occurred_at, str):
        occurred = occurred_at
    else:
        occurred = (occurred_at or datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)).isoformat()
    return {
        "event_id": event_id,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": occurred,
        "tenant_id": tenant_id,
        "user_subject_id": user_subject_id,
        "session_id": session_id,
        "interaction_id": interaction_id,
        "trace_id": trace_id,
        "event_type": event_type,
    }


def interaction_started(
    event_id: str,
    *,
    capability: str | None = "FR-001",
    entrypoint: str = "api",
    role_category: str = "employee",
    **kwargs: Any,
) -> dict[str, object]:
    return {
        **_envelope(event_id, "interaction_started", **kwargs),
        "capability": capability,
        "entrypoint": entrypoint,
        "role_category": role_category,
    }


def interaction_completed(
    event_id: str,
    *,
    status: str = "completed",
    duration_ms: int = 1200,
    mes_duration_ms: int = 800,
    llm_duration_ms: int = 300,
    local_duration_ms: int = 100,
    result_rows_bucket: str = "1-10",
    error_category: str | None = None,
    **kwargs: Any,
) -> dict[str, object]:
    return {
        **_envelope(event_id, "interaction_completed", **kwargs),
        "status": status,
        "duration_ms": duration_ms,
        "mes_duration_ms": mes_duration_ms,
        "llm_duration_ms": llm_duration_ms,
        "local_duration_ms": local_duration_ms,
        "result_rows_bucket": result_rows_bucket,
        "error_category": error_category,
    }


def llm_call_completed(
    event_id: str,
    *,
    logical_call_id: str = "call-1",
    stage: str = "extract",
    model_alias: str = "factory-fast",
    actual_model: str = "qwen3-32b",
    attempt: int = 1,
    prompt_tokens: int = 120,
    completion_tokens: int = 40,
    cached_tokens: int = 10,
    reasoning_tokens: int = 0,
    duration_ms: int = 300,
    status: str = "completed",
    fallback_reason: str | None = None,
    error_category: str | None = None,
    **kwargs: Any,
) -> dict[str, object]:
    return {
        **_envelope(event_id, "llm_call_completed", **kwargs),
        "logical_call_id": logical_call_id,
        "stage": stage,
        "model_alias": model_alias,
        "actual_model": actual_model,
        "attempt": attempt,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "duration_ms": duration_ms,
        "status": status,
        "fallback_reason": fallback_reason,
        "error_category": error_category,
    }


def mes_call_completed(
    event_id: str,
    *,
    operation_id: str = "GongziMxQuery",
    page_count: int = 1,
    row_count_bucket: str = "1-10",
    duration_ms: int = 200,
    status: str = "completed",
    error_category: str | None = None,
    **kwargs: Any,
) -> dict[str, object]:
    return {
        **_envelope(event_id, "mes_call_completed", **kwargs),
        "operation_id": operation_id,
        "page_count": page_count,
        "row_count_bucket": row_count_bucket,
        "duration_ms": duration_ms,
        "status": status,
        "error_category": error_category,
    }
