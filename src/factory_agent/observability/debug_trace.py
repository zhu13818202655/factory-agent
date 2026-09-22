"""B-channel debug payload capture (ADR-0004 §Environment Tiers).

Two content routes exist, and this module is the second one:

* **A — the fact tables.** Stage, offset, duration, status, token counts and
  row-count buckets. Legal in every environment; no business values.
* **B — the ``debug_trace`` store.** Prompt text, tool parameters and result
  envelopes, kept in a store of its own with its own retention and access
  control. Legal only in a developer environment *and* behind the explicit
  ``debug_trace_enabled`` switch.

Why the payload is captured here rather than derived from logs: the debug
channel must be reachable without the log stream. A prompt that is dropped
because it fell below a log level is a prompt a developer cannot debug with.

**Redaction is deliberately narrower than the log policy.** ``redaction.py``
withholds ``prompt`` / ``answer`` / ``salary`` / ``amount`` by *key*, which is
exactly the business content this channel exists to show. So a captured payload
keeps business values and withholds only credential material — app key,
``sign``, access token, cookie, password, DSN. That trade is the documented
exception of the plan's §4.4: it is a ``sensitive-field classification`` change
under ``AGENTS.md`` §Security Stop Conditions and ships with the ADR note.

The capture site is a context variable opened once per interaction, mirroring
``application.usage``'s MES buffer: adapters record payloads without knowing
the ownership pair, and the identity is attached at drain time.
"""

import json
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.observability.redaction import REDACTED, redact_text

_LOGGER = get_logger("factory_agent.observability.debug_trace")

CaptureKind = Literal["llm", "mes"]

#: Credential-shaped key fragments withheld from a captured payload. Narrower
#: than ``redaction.SENSITIVE_KEY_PATTERNS`` on purpose (see the module
#: docstring): every entry is material that authenticates a call, not business
#: content. Matching is by substring so ``apiKey``/``app_key``/``X-Sign`` all hit.
_CREDENTIAL_KEY_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "appkey",
    "app_key",
    "app-key",
    "sign",
    "access_key",
    "accesskey",
    "private_key",
    "privatekey",
    "dsn",
    "postgres_url",
    "redis_url",
)

#: LLM usage counters are numbers (token *counts*), not credentials — the
#: ``token`` substring above would otherwise redact ``prompt_tokens`` & co.
#: Listed explicitly (not ``endswith("tokens")``) so a credential named
#: ``..._tokens`` still has to fight its way past this exemption on purpose.
_USAGE_COUNTER_KEYS: frozenset[str] = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)

#: Strings longer than this are cut even inside a payload that is under the byte
#: cap, so one runaway field cannot crowd out the rest of the structure.
_MAX_STRING_CHARS = 4_000

#: Rows sampled from an over-cap list, so the shape of a record stays visible
#: after the detail is dropped.
_TRUNCATED_SAMPLE_ROWS = 1

#: Marker keys of a structure-only payload. Underscore-prefixed so they cannot
#: be mistaken for a business field of the same name.
_COUNT_KEY = "_truncated_items"
_SAMPLE_KEY = "_sample"


@dataclass(frozen=True, slots=True)
class CaptureScope:
    """Ownership pair of the interaction currently executing in this context."""

    tenant_id: str
    user_id: str
    session_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class CapturedPayload:
    """One span's content, already redacted and bounded.

    ``truncated`` is part of the contract, not a hint: a consumer that renders
    a truncated payload as if it were complete reaches the wrong conclusion
    during exactly the incident it is debugging (§4.7 决策 C).
    """

    input: object
    output: object
    truncated: bool = False
    original_rows: int | None = None
    original_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class DebugTraceCapture:
    """One span's payload plus the identity the span belongs to."""

    span_key: str
    kind: CaptureKind
    tenant_id: str
    user_id: str
    session_id: str
    interaction_id: str
    payload: CapturedPayload
    occurred_at: datetime
    stage: str | None = None
    logical_call_id: str | None = None
    attempt: int | None = None
    operation_id: str | None = None


class DebugTraceWriter(Protocol):
    """Durable sink for drained captures (implemented by the store)."""

    async def write(self, captures: Sequence[DebugTraceCapture]) -> None: ...


@dataclass(frozen=True, slots=True)
class DebugTraceConfig:
    """Effective capture settings; immutable once installed."""

    enabled: bool = False
    max_payload_bytes: int = 262_144
    max_rows: int = 500


#: Closed by default: a process that forgets to configure captures nothing, so
#: there is no "capture by omission" state.
_config = DebugTraceConfig()

_scope_var: ContextVar["CaptureScope | None"] = ContextVar(
    "factory_agent_debug_scope", default=None
)
_buffer_var: ContextVar["list[DebugTraceCapture] | None"] = ContextVar(
    "factory_agent_debug_captures", default=None
)


def configure_debug_trace(
    *,
    enabled: bool,
    max_payload_bytes: int,
    max_rows: int,
) -> None:
    """Install the effective capture settings. Called once at startup.

    The caller is expected to pass an already-gated ``enabled`` (the settings
    object's ``debug_trace_active``), so the environment ceiling and the runtime
    switch are resolved in one place rather than re-derived here.
    """
    global _config  # noqa: PLW0603 - single startup-time assignment
    _config = DebugTraceConfig(
        enabled=enabled, max_payload_bytes=max_payload_bytes, max_rows=max_rows
    )


def debug_capture_enabled() -> bool:
    """Whether this process may build payloads at all.

    Adapters branch on this *before* assembling a payload: with the switch
    closed the payload is never constructed, so no code path can merely forget
    to drop it (§2.5 P1 — the capability is absent, not disabled).
    """
    return _config.enabled


def open_capture_scope(scope: CaptureScope) -> None:
    """Bind the interaction whose spans this context will capture."""
    _scope_var.set(scope)
    _buffer_var.set([])


def close_capture_scope() -> None:
    """Close the scope and drop whatever it had not drained."""
    _scope_var.set(None)
    _buffer_var.set(None)


def current_capture_scope() -> CaptureScope | None:
    return _scope_var.get()


def drain_debug_captures() -> tuple[DebugTraceCapture, ...]:
    """Take and clear the pending captures of the current interaction.

    Mirrors ``application.usage.drain_mes_events``: the buffer stays open, so a
    capture recorded after an earlier drain still reaches the next one.
    """
    captures = _buffer_var.get()
    if captures is None:
        return ()
    drained = tuple(captures)
    captures.clear()
    return drained


def record_debug_capture(
    *,
    span_key: str,
    kind: CaptureKind,
    input_payload: object,
    output_payload: object,
    occurred_at: datetime | None = None,
    stage: str | None = None,
    logical_call_id: str | None = None,
    attempt: int | None = None,
    operation_id: str | None = None,
) -> None:
    """Capture one span's payload, redacted and bounded.

    Never raises and never blocks: outside a capture scope (a health probe, or
    a test driving an adapter directly) the call is dropped, and a
    summarisation fault is logged rather than propagated into the call it
    describes.
    """
    scope = _scope_var.get()
    buffer = _buffer_var.get()
    if scope is None or buffer is None or not _config.enabled:
        return
    try:
        payload = summarise_payload(
            input_payload,
            output_payload,
            max_payload_bytes=_config.max_payload_bytes,
            max_rows=_config.max_rows,
        )
    except Exception:  # noqa: BLE001 - capture must never break the call
        _LOGGER.exception("debug_trace.summarise_failed", span_key=span_key, kind=kind)
        return
    buffer.append(
        DebugTraceCapture(
            span_key=span_key,
            kind=kind,
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            session_id=scope.session_id,
            interaction_id=scope.interaction_id,
            payload=payload,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            stage=stage,
            logical_call_id=logical_call_id,
            attempt=attempt,
            operation_id=operation_id,
        )
    )


def llm_span_key(logical_call_id: str) -> str:
    """Join key shared by an LLM fact row and its captured payload."""
    return f"llm:{logical_call_id}"


def mes_span_key(operation_id: str, started_at: datetime) -> str:
    """Join key shared by a MES fact row and its captured payload.

    Built from the call's start instant, which both sides derive from the same
    clock reading, so the two records match without a shared sequence number.
    """
    return f"mes:{operation_id}:{utc_isoformat(started_at)}"


def utc_isoformat(value: datetime) -> str:
    """Render an instant as UTC ISO-8601, the form both writers persist."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def summarise_payload(
    input_payload: object,
    output_payload: object,
    *,
    max_payload_bytes: int,
    max_rows: int,
) -> CapturedPayload:
    """Redact and bound one span's content.

    Over either cap the payload degrades to *structure plus statistics* — list
    contents become a count and one sample row, long strings are cut — and
    ``truncated`` is set, because a silently shortened payload reads as a
    complete one (§4.7 决策 C).
    """
    redacted_input = _redact(input_payload)
    redacted_output = _redact(output_payload)
    rows = _count_rows(redacted_input) + _count_rows(redacted_output)
    size = _measure(redacted_input) + _measure(redacted_output)
    if size <= max_payload_bytes and rows <= max_rows:
        return CapturedPayload(
            input=redacted_input,
            output=redacted_output,
            truncated=False,
            original_rows=None,
            original_bytes=size,
        )
    return CapturedPayload(
        input=_structure_only(redacted_input),
        output=_structure_only(redacted_output),
        truncated=True,
        original_rows=rows,
        original_bytes=size,
    )


def _redact(value: object) -> object:
    """Withhold credential material, keep business content."""
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        redacted: dict[str, object] = {}
        for key, item in mapping.items():
            name = str(key)
            redacted[name] = REDACTED if _is_credential_key(name) else _redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, str):
        # Value-level scrub: a credential also arrives inside free text — a DSN,
        # a Bearer header, a signed URL's query string.
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def _is_credential_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _USAGE_COUNTER_KEYS:
        return False
    return any(pattern in lowered for pattern in _CREDENTIAL_KEY_PATTERNS)


def _structure_only(value: object) -> object:
    """Replace detail with shape: counts for lists, cuts for long strings."""
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {str(key): _structure_only(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = list(cast("Sequence[object]", value))
        if not items:
            return []
        sample = [_structure_only(item) for item in items[:_TRUNCATED_SAMPLE_ROWS]]
        return [{_COUNT_KEY: len(items), _SAMPLE_KEY: sample}]
    if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
        return value[:_MAX_STRING_CHARS] + "…"
    return value


def _count_rows(value: object) -> int:
    """Total list items anywhere in the payload; the row budget's basis."""
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return sum(_count_rows(item) for item in mapping.values())
    if isinstance(value, (list, tuple)):
        return sum(1 + _count_rows(item) for item in cast("Sequence[object]", value))
    return 0


def _measure(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CaptureScope",
    "CapturedPayload",
    "DebugTraceCapture",
    "DebugTraceConfig",
    "DebugTraceWriter",
    "close_capture_scope",
    "configure_debug_trace",
    "current_capture_scope",
    "debug_capture_enabled",
    "drain_debug_captures",
    "llm_span_key",
    "mes_span_key",
    "open_capture_scope",
    "record_debug_capture",
    "summarise_payload",
    "utc_isoformat",
]
