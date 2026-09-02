"""Read-side fact row types for the metering tables.

The metering tables (``interaction_fact`` / ``llm_call_fact`` /
``mes_call_fact``) are owned and written by factory-agent inside its business
transaction; this service reads them read-only for platform
reporting. The dataclasses here mirror one row of each table.

Payload validation happens on the writer side (factory-agent) before a row is
stored; there is no ingest path in this service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class InteractionFact:
    event_id: str
    tenant_id: str
    session_id: str
    interaction_id: str
    event_type: str
    user_subject_id: str
    occurred_at: datetime
    capability_id: str | None
    entrypoint: str | None
    role_category: str | None
    status: str | None
    duration_ms: int | None
    mes_duration_ms: int | None
    llm_duration_ms: int | None
    local_duration_ms: int | None
    result_rows_bucket: str | None
    error_category: str | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class LlmCallFact:
    event_id: str
    tenant_id: str
    session_id: str
    interaction_id: str
    occurred_at: datetime
    logical_call_id: str
    stage: str
    model_alias: str
    actual_model: str
    attempt: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    duration_ms: int
    status: str
    fallback_reason: str | None
    error_category: str | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class MesCallFact:
    """One customer MES request per row (read-only for this service).

    ``operation_id`` carries no URL or business parameter values; the billing
    category is resolved at query time from ``mes_operation_category`` (R1:
    API classification, never capability classification).
    """

    event_id: str
    tenant_id: str
    session_id: str
    interaction_id: str
    occurred_at: datetime
    operation_id: str
    page_count: int
    row_count_bucket: str | None
    duration_ms: int
    status: str
    error_category: str | None
    received_at: datetime


__all__ = ["InteractionFact", "LlmCallFact", "MesCallFact"]
