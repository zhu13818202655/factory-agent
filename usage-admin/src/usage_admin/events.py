"""Event contract whitelist, canonical digest, and fact extraction.

The whitelist mirrors ``contracts/usage-events/v1``. usage-admin never stores
prompts, answers, employee identities, wage/output/order values, ``DataScope``
ID lists, or credentials; the whitelist is the first gate that keeps them out
(``unevaluatedProperties: false`` semantics).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

ENVELOPE_REQUIRED: frozenset[str] = frozenset(
    {
        "event_id",
        "schema_version",
        "occurred_at",
        "tenant_id",
        "user_subject_id",
        "session_id",
        "interaction_id",
        "trace_id",
        "event_type",
    }
)

#: Optional fields shared by every event envelope.
ENVELOPE_OPTIONAL: frozenset[str] = frozenset({"received_at"})

_REQUIRED_BY_TYPE: dict[str, frozenset[str]] = {
    "interaction_started": frozenset({"capability", "entrypoint", "role_category"}),
    "interaction_completed": frozenset(
        {
            "status",
            "duration_ms",
            "mes_duration_ms",
            "llm_duration_ms",
            "local_duration_ms",
            "result_rows_bucket",
            "error_category",
        }
    ),
    "llm_call_completed": frozenset(
        {
            "logical_call_id",
            "stage",
            "model_alias",
            "actual_model",
            "attempt",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "duration_ms",
            "status",
            "fallback_reason",
            "error_category",
        }
    ),
    "mes_call_completed": frozenset(
        {
            "operation_id",
            "page_count",
            "row_count_bucket",
            "duration_ms",
            "status",
            "error_category",
        }
    ),
    "artifact_generated": frozenset({"format", "size_bucket", "status"}),
    "artifact_downloaded": frozenset({"artifact_id", "status"}),
}

_OPTIONAL_BY_TYPE: dict[str, frozenset[str]] = {
    "interaction_started": frozenset(),
    "interaction_completed": frozenset(),
    "llm_call_completed": frozenset(),
    "mes_call_completed": frozenset(),
    "artifact_generated": frozenset(),
    "artifact_downloaded": frozenset(),
}

#: Event types the platform will accept and roll up.
SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(_REQUIRED_BY_TYPE)


def required_fields(event_type: str) -> frozenset[str]:
    return ENVELOPE_REQUIRED | _REQUIRED_BY_TYPE.get(event_type, frozenset())


def allowed_fields(event_type: str) -> frozenset[str]:
    return ENVELOPE_REQUIRED | ENVELOPE_OPTIONAL | _REQUIRED_BY_TYPE.get(event_type, frozenset())


def validate_event(event: object) -> str | None:
    """Return a human-readable rejection reason, or ``None`` when acceptable."""
    if not isinstance(event, dict):
        return "event must be a JSON object"
    payload = cast("dict[str, object]", event)

    event_type = payload.get("event_type")
    if not isinstance(event_type, str):
        return "event_type must be a string"
    if event_type not in SUPPORTED_EVENT_TYPES:
        return f"unsupported event_type {event_type!r}"

    unknown = set(payload) - allowed_fields(event_type)
    if unknown:
        return f"unknown fields {sorted(unknown)!r}"
    missing = required_fields(event_type) - set(payload)
    if missing:
        return f"missing fields {sorted(missing)!r}"

    for field, expected in _TYPE_CHECKS:
        value = payload.get(field)
        if value is not None and not isinstance(value, expected):
            return f"field {field!r} has the wrong type"
    return None


#: Type checks applied to the whitelisted scalar fields. ``received_at`` is set
#: by the platform, so it is validated as a string when present.
_TYPE_CHECKS: tuple[tuple[str, type], ...] = (
    ("event_id", str),
    ("schema_version", str),
    ("occurred_at", str),
    ("received_at", str),
    ("tenant_id", str),
    ("user_subject_id", str),
    ("session_id", str),
    ("interaction_id", str),
    ("trace_id", str),
    ("event_type", str),
    ("capability", str),
    ("entrypoint", str),
    ("role_category", str),
    ("status", str),
    ("duration_ms", int),
    ("mes_duration_ms", int),
    ("llm_duration_ms", int),
    ("local_duration_ms", int),
    ("result_rows_bucket", str),
    ("error_category", str),
    ("logical_call_id", str),
    ("stage", str),
    ("model_alias", str),
    ("actual_model", str),
    ("attempt", int),
    ("prompt_tokens", int),
    ("completion_tokens", int),
    ("cached_tokens", int),
    ("reasoning_tokens", int),
    ("fallback_reason", str),
    ("operation_id", str),
    ("page_count", int),
    ("row_count_bucket", str),
    ("format", str),
    ("size_bucket", str),
    ("artifact_id", str),
)


def canonical_digest(payload: dict[str, object]) -> str:
    """Deterministic SHA-256 over the canonical JSON payload.

    Equal digests mean the exact same event is being redelivered; a different
    digest under the same ``event_id`` is a conflict that must be rejected.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_occurred_at(payload: dict[str, object]) -> datetime:
    raw = payload["occurred_at"]
    if not isinstance(raw, str):
        raise ValueError("occurred_at must be a string")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


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


def _field(payload: dict[str, object], name: str) -> str:
    value = payload[name]
    return value if isinstance(value, str) else ""


def _int_field(payload: dict[str, object], name: str) -> int:
    value = payload[name]
    return value if isinstance(value, int) else 0


def _opt_field(payload: dict[str, object], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) else None


def _opt_int_field(payload: dict[str, object], name: str) -> int | None:
    value = payload.get(name)
    return value if isinstance(value, int) else None


def to_interaction_fact(payload: dict[str, object], *, received_at: datetime) -> InteractionFact:
    return InteractionFact(
        event_id=_field(payload, "event_id"),
        tenant_id=_field(payload, "tenant_id"),
        session_id=_field(payload, "session_id"),
        interaction_id=_field(payload, "interaction_id"),
        event_type=_field(payload, "event_type"),
        user_subject_id=_field(payload, "user_subject_id"),
        occurred_at=parse_occurred_at(payload),
        capability_id=_opt_field(payload, "capability"),
        entrypoint=_opt_field(payload, "entrypoint"),
        role_category=_opt_field(payload, "role_category"),
        status=_opt_field(payload, "status"),
        duration_ms=_opt_int_field(payload, "duration_ms"),
        mes_duration_ms=_opt_int_field(payload, "mes_duration_ms"),
        llm_duration_ms=_opt_int_field(payload, "llm_duration_ms"),
        local_duration_ms=_opt_int_field(payload, "local_duration_ms"),
        result_rows_bucket=_opt_field(payload, "result_rows_bucket"),
        error_category=_opt_field(payload, "error_category"),
        received_at=received_at,
    )


def to_llm_call_fact(payload: dict[str, object], *, received_at: datetime) -> LlmCallFact:
    return LlmCallFact(
        event_id=_field(payload, "event_id"),
        tenant_id=_field(payload, "tenant_id"),
        session_id=_field(payload, "session_id"),
        interaction_id=_field(payload, "interaction_id"),
        occurred_at=parse_occurred_at(payload),
        logical_call_id=_field(payload, "logical_call_id"),
        stage=_field(payload, "stage"),
        model_alias=_field(payload, "model_alias"),
        actual_model=_field(payload, "actual_model"),
        attempt=_int_field(payload, "attempt"),
        prompt_tokens=_int_field(payload, "prompt_tokens"),
        completion_tokens=_int_field(payload, "completion_tokens"),
        cached_tokens=_int_field(payload, "cached_tokens"),
        reasoning_tokens=_int_field(payload, "reasoning_tokens"),
        duration_ms=_int_field(payload, "duration_ms"),
        status=_field(payload, "status"),
        fallback_reason=_opt_field(payload, "fallback_reason"),
        error_category=_opt_field(payload, "error_category"),
        received_at=received_at,
    )


def to_mes_call_fact(payload: dict[str, object], *, received_at: datetime) -> MesCallFact:
    return MesCallFact(
        event_id=_field(payload, "event_id"),
        tenant_id=_field(payload, "tenant_id"),
        session_id=_field(payload, "session_id"),
        interaction_id=_field(payload, "interaction_id"),
        occurred_at=parse_occurred_at(payload),
        operation_id=_field(payload, "operation_id"),
        page_count=_int_field(payload, "page_count"),
        row_count_bucket=_opt_field(payload, "row_count_bucket"),
        duration_ms=_int_field(payload, "duration_ms"),
        status=_field(payload, "status"),
        error_category=_opt_field(payload, "error_category"),
        received_at=received_at,
    )


__all__ = [
    "InteractionFact",
    "LlmCallFact",
    "MesCallFact",
    "SUPPORTED_EVENT_TYPES",
    "allowed_fields",
    "canonical_digest",
    "parse_occurred_at",
    "required_fields",
    "to_interaction_fact",
    "to_llm_call_fact",
    "to_mes_call_fact",
    "validate_event",
]
