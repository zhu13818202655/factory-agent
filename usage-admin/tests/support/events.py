"""Test helpers for building metering fact rows directly.

usage-admin no longer ingests raw events (Story 11: factory-agent writes the
metering tables in a separate transaction after its business commit), so these
helpers construct
the ``InteractionFact`` / ``LlmCallFact`` / ``MesCallFact`` rows that the
in-memory store reads, mirroring what the removed ingest path used to produce.
"""

from __future__ import annotations

from datetime import datetime, timezone

from usage_admin.events import InteractionFact, LlmCallFact, MesCallFact

DEFAULT_OCCURRED_AT = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


def interaction_started(
    event_id: str,
    *,
    capability: str | None = "FR-001",
    entrypoint: str = "api",
    role_category: str = "employee",
    tenant_id: str = "tenant-a",
    user_subject_id: str | None = None,
    session_id: str = "session-1",
    interaction_id: str = "interaction-1",
    occurred_at: datetime | None = None,
) -> InteractionFact:
    if user_subject_id is None:
        user_subject_id = "a" * 64
    at = occurred_at or DEFAULT_OCCURRED_AT
    return InteractionFact(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id=session_id,
        interaction_id=interaction_id,
        event_type="interaction_started",
        user_subject_id=user_subject_id,
        occurred_at=at,
        capability_id=capability,
        entrypoint=entrypoint,
        role_category=role_category,
        status=None,
        duration_ms=None,
        mes_duration_ms=None,
        llm_duration_ms=None,
        local_duration_ms=None,
        result_rows_bucket=None,
        error_category=None,
        received_at=at,
    )


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
    tenant_id: str = "tenant-a",
    user_subject_id: str | None = None,
    session_id: str = "session-1",
    interaction_id: str = "interaction-1",
    occurred_at: datetime | None = None,
) -> InteractionFact:
    if user_subject_id is None:
        user_subject_id = "a" * 64
    at = occurred_at or DEFAULT_OCCURRED_AT
    return InteractionFact(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id=session_id,
        interaction_id=interaction_id,
        event_type="interaction_completed",
        user_subject_id=user_subject_id,
        occurred_at=at,
        capability_id=None,
        entrypoint=None,
        role_category=None,
        status=status,
        duration_ms=duration_ms,
        mes_duration_ms=mes_duration_ms,
        llm_duration_ms=llm_duration_ms,
        local_duration_ms=local_duration_ms,
        result_rows_bucket=result_rows_bucket,
        error_category=error_category,
        received_at=at,
    )


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
    tenant_id: str = "tenant-a",
    user_subject_id: str | None = None,
    session_id: str = "session-1",
    interaction_id: str = "interaction-1",
    occurred_at: datetime | None = None,
) -> LlmCallFact:
    if user_subject_id is None:
        user_subject_id = "a" * 64
    at = occurred_at or DEFAULT_OCCURRED_AT
    return LlmCallFact(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id=session_id,
        interaction_id=interaction_id,
        occurred_at=at,
        logical_call_id=logical_call_id,
        stage=stage,
        model_alias=model_alias,
        actual_model=actual_model,
        attempt=attempt,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        duration_ms=duration_ms,
        status=status,
        fallback_reason=fallback_reason,
        error_category=error_category,
        received_at=at,
    )


def mes_call_completed(
    event_id: str,
    *,
    operation_id: str = "GongziMxQuery",
    page_count: int = 1,
    row_count_bucket: str = "1-10",
    duration_ms: int = 200,
    status: str = "completed",
    error_category: str | None = None,
    tenant_id: str = "tenant-a",
    session_id: str = "session-1",
    interaction_id: str = "interaction-1",
    occurred_at: datetime | None = None,
) -> MesCallFact:
    at = occurred_at or DEFAULT_OCCURRED_AT
    return MesCallFact(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id=session_id,
        interaction_id=interaction_id,
        occurred_at=at,
        operation_id=operation_id,
        page_count=page_count,
        row_count_bucket=row_count_bucket,
        duration_ms=duration_ms,
        status=status,
        error_category=error_category,
        received_at=at,
    )
