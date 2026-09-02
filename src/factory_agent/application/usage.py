"""Usage event construction for the direct-to-table metering write.

Events carry only the whitelisted fields of the local archive-payload format
(``SCHEMA_VERSION`` below). Prompts, completions, result rows, and scope ID
lists never enter an event; the user is identified by an irreversible
per-tenant pseudonym.

Since Story 11 the events are written by the owning service directly into the
``usage_event`` / ``*_fact`` tables in a separate transaction after the business
commit; the payload shape is validated before write. A metering failure is
alerted and never blocks the interaction.
"""

from __future__ import annotations

import hashlib
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
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
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports import MesCallRecord, ModelStage, UsageEvent

_LOGGER = get_logger("factory_agent.application.usage")

SCHEMA_VERSION = "1.0"

Entrypoint = Literal["api", "web", "mobile"]
CompletionStatus = Literal["completed", "failed", "cancelled", "rejected"]
MesCallStatus = Literal["completed", "failed"]

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

#: Active interaction usage context, set by the session pipeline while one turn
#: is executing. The MES adapter reads it at its single ``_send`` exit to build
#: ``mes_call_completed`` events without threading identifiers through every
#: executor layer (Story 11 2.6).
_usage_context_var: ContextVar[UsageContext | None] = ContextVar(
    "factory_agent_usage_context", default=None
)

#: Per-interaction collection of MES call events. ``record_mes_call`` appends
#: into the active list; the session pipeline drains it at commit time. Kept on
#: a separate ContextVar so concurrent interactions never share a buffer.
_mes_events_var: ContextVar[list[UsageEvent] | None] = ContextVar(
    "factory_agent_mes_events", default=None
)


def set_usage_context(context: UsageContext | None) -> None:
    if context is None:
        _mes_events_var.set(None)
    elif _mes_events_var.get() is None:
        _mes_events_var.set([])
    _usage_context_var.set(context)


def current_usage_context() -> UsageContext | None:
    return _usage_context_var.get()


def drain_mes_events() -> tuple[UsageEvent, ...]:
    """Take and clear the pending MES events of the current interaction.

    The buffer stays open so events recorded after an earlier commit are still
    captured by the next one; it is closed by ``set_usage_context(None)``.
    """
    events = _mes_events_var.get()
    if events is None:
        return ()
    drained = list(events)
    events.clear()
    return tuple(drained)


def record_mes_call(call: MesCallRecord) -> None:
    """Record one MES call into the active interaction, if any.

    Idempotent and exception-safe by contract: outside a metered interaction
    (readiness probes, unresolved scope) the call is dropped; a construction
    failure is logged and never propagates into the MES adapter (Story 11 1.6).
    """
    context = current_usage_context()
    events = _mes_events_var.get()
    if context is None or events is None:
        return
    try:
        events.append(
            mes_call_completed_event(
                context,
                occurred_at=datetime.now(timezone.utc),
                operation_id=call.operation_id,
                page_count=call.page_count,
                row_count=call.row_count,
                duration_ms=call.duration_ms,
                status=call.status,
                error_category=call.error_category,
            )
        )
    except Exception:  # noqa: BLE001 - metering must never break the adapter
        _LOGGER.exception("usage.mes_record_failed")


class ContextVarMesCallRecorder:
    """``MesCallRecorder`` implementation routing into the metering context.

    The single shared instance is injected into both the MES adapter and the
    session pipeline; per-interaction isolation comes from the ContextVar, so
    concurrent interactions never interleave records.
    """

    def record(self, call: MesCallRecord) -> None:
        record_mes_call(call)


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
) -> UsageEvent:
    """Role is display-only (M11); recorded as a category, never a gate."""
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
) -> UsageEvent:
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
) -> UsageEvent:
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


def mes_call_completed_event(
    context: UsageContext,
    *,
    occurred_at: datetime,
    operation_id: str,
    page_count: int,
    row_count: int,
    duration_ms: int,
    status: MesCallStatus,
    error_category: str | None = None,
) -> UsageEvent:
    """One MES HTTP call completion event (Story 11 2.5).

    Fields follow the local archive-payload format (``SCHEMA_VERSION``) and
    never carry a URL, business parameter value, or credential. ``page_count``
    is the request page number within its paged fetch (1 for non-paged calls)
    and is a supporting metric — call counts are aggregated by event count, not
    by summing ``page_count`` (D6).
    """
    payload = context.envelope("mes_call_completed", occurred_at)
    payload["operation_id"] = operation_id
    payload["page_count"] = max(0, int(page_count))
    payload["row_count_bucket"] = row_count_bucket(max(0, row_count))
    payload["duration_ms"] = max(0, int(duration_ms))
    payload["status"] = status
    payload["error_category"] = _short(error_category)
    return _wrap(context, payload, occurred_at)


def _wrap(context: UsageContext, payload: dict[str, object], occurred_at: datetime) -> UsageEvent:
    event_id = payload["event_id"]
    event_type = payload["event_type"]
    if not isinstance(event_id, str) or not isinstance(event_type, str):
        raise ValueError("usage event envelope is malformed")
    return UsageEvent(
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
    "ContextVarMesCallRecorder",
    "Entrypoint",
    "MesCallStatus",
    "UsageContext",
    "completion_status",
    "current_usage_context",
    "drain_mes_events",
    "interaction_completed_event",
    "interaction_started_event",
    "llm_call_event",
    "mes_call_completed_event",
    "new_trace_id",
    "pseudonymous_subject",
    "record_mes_call",
    "row_count_bucket",
    "set_usage_context",
]
