"""Usage event construction for the transactional outbox.

Events carry only the whitelisted fields defined by ``contracts/usage-events/v1``.
Prompts, completions, result rows, and scope ID lists never enter an event; the
user is identified by an irreversible per-tenant pseudonym.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionStatus,
    Role,
    SessionId,
    TenantId,
    UserId,
)
from factory_agent.ports import ModelStage, UsageOutboxEvent

SCHEMA_VERSION = "1.0"

Entrypoint = Literal["api", "web", "mobile"]
CompletionStatus = Literal["completed", "failed", "cancelled", "rejected"]

_ROW_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (10, "1-10"),
    (100, "11-100"),
    (1000, "101-1000"),
)

_INTERACTION_STATUS_MAP: dict[InteractionStatus, CompletionStatus] = {
    InteractionStatus.COMPLETED: "completed",
    InteractionStatus.FAILED: "failed",
    InteractionStatus.CANCELLED: "cancelled",
}


def pseudonymous_subject(tenant_id: TenantId, user_id: UserId) -> str:
    """Irreversible per-tenant user pseudonym; never the raw user identifier."""
    return hashlib.sha256(f"{tenant_id}\x1f{user_id}".encode()).hexdigest()


def row_count_bucket(row_count: int) -> str:
    for upper, label in _ROW_BUCKETS:
        if row_count <= upper:
            return label
    return "1001+"


def completion_status(status: InteractionStatus) -> CompletionStatus:
    return _INTERACTION_STATUS_MAP.get(status, "failed")


@dataclass(frozen=True, slots=True)
class UsageContext:
    """Stable identifiers shared by every event of one interaction."""

    tenant_id: TenantId
    user_id: UserId
    session_id: SessionId
    interaction_id: InteractionId
    trace_id: str

    def envelope(self, event_type: str, occurred_at: datetime) -> dict[str, object]:
        return {
            "event_id": str(uuid.uuid4()),
            "schema_version": SCHEMA_VERSION,
            "occurred_at": occurred_at.isoformat(),
            "tenant_id": str(self.tenant_id),
            "user_subject_id": pseudonymous_subject(self.tenant_id, self.user_id),
            "session_id": str(self.session_id),
            "interaction_id": str(self.interaction_id),
            "trace_id": self.trace_id,
            "event_type": event_type,
        }


def new_trace_id() -> str:
    return uuid.uuid4().hex


def interaction_started_event(
    context: UsageContext,
    *,
    occurred_at: datetime,
    capability: CapabilityId | None,
    entrypoint: Entrypoint,
    role: Role,
) -> UsageOutboxEvent:
    payload = context.envelope("interaction_started", occurred_at)
    payload["capability"] = str(capability) if capability is not None else None
    payload["entrypoint"] = entrypoint
    payload["role_category"] = role.value
    return _wrap(context, payload, occurred_at)


def interaction_completed_event(
    context: UsageContext,
    *,
    occurred_at: datetime,
    status: CompletionStatus,
    duration_ms: int,
    mes_duration_ms: int,
    llm_duration_ms: int,
    local_duration_ms: int,
    result_row_count: int,
    error_category: str | None,
) -> UsageOutboxEvent:
    payload = context.envelope("interaction_completed", occurred_at)
    payload["status"] = status
    payload["duration_ms"] = max(0, duration_ms)
    payload["mes_duration_ms"] = max(0, mes_duration_ms)
    payload["llm_duration_ms"] = max(0, llm_duration_ms)
    payload["local_duration_ms"] = max(0, local_duration_ms)
    payload["result_rows_bucket"] = row_count_bucket(max(0, result_row_count))
    payload["error_category"] = _short(error_category)
    return _wrap(context, payload, occurred_at)


def llm_call_event(
    context: UsageContext,
    *,
    occurred_at: datetime,
    logical_call_id: str,
    stage: ModelStage,
    model_alias: str,
    actual_model: str,
    attempt: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    duration_ms: int = 0,
    status: Literal["completed", "failed"] = "completed",
    fallback_reason: str | None = None,
    error_category: str | None = None,
) -> UsageOutboxEvent:
    payload = context.envelope("llm_call_completed", occurred_at)
    payload["logical_call_id"] = logical_call_id
    payload["stage"] = stage.value
    payload["model_alias"] = model_alias
    payload["actual_model"] = actual_model
    payload["attempt"] = max(1, attempt)
    payload["prompt_tokens"] = max(0, prompt_tokens)
    payload["completion_tokens"] = max(0, completion_tokens)
    payload["cached_tokens"] = max(0, cached_tokens)
    payload["reasoning_tokens"] = max(0, reasoning_tokens)
    payload["duration_ms"] = max(0, duration_ms)
    payload["status"] = status
    payload["fallback_reason"] = _short(fallback_reason)
    payload["error_category"] = _short(error_category)
    return _wrap(context, payload, occurred_at)


def _wrap(
    context: UsageContext, payload: dict[str, object], occurred_at: datetime
) -> UsageOutboxEvent:
    event_id = payload["event_id"]
    event_type = payload["event_type"]
    if not isinstance(event_id, str) or not isinstance(event_type, str):
        raise ValueError("usage event envelope is malformed")
    return UsageOutboxEvent(
        event_id=event_id,
        event_type=event_type,
        tenant_id=context.tenant_id,
        payload=payload,
        created_at=occurred_at,
    )


def _short(value: str | None) -> str | None:
    return value[:64] if value else None


__all__ = [
    "SCHEMA_VERSION",
    "CompletionStatus",
    "Entrypoint",
    "UsageContext",
    "completion_status",
    "interaction_completed_event",
    "interaction_started_event",
    "llm_call_event",
    "new_trace_id",
    "pseudonymous_subject",
    "row_count_bucket",
]
