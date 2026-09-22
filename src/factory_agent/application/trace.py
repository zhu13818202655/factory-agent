"""Trace read model: stitch the carriers into one timeline (§4.6).

The carriers already existed (``agent_interaction``, ``agent_interaction_event``,
``llm_call_fact``, ``mes_call_fact``, ``interaction_fact``); what was missing was
a single reading that a view can render without re-deriving anything. That is
this module.

Three contract decisions are encoded here, and each one exists to stop a
consumer from guessing:

* ``content_capture`` **declares** whether a ``payload`` section can appear. The
  presence of payload data never selects the shape — otherwise a configuration
  change would silently alter a response's structure.
* ``truncated`` is carried through verbatim. A payload that was shortened must
  not read as a complete one: that is the failure mode that makes a developer
  reach the wrong conclusion during exactly the incident under investigation.
* ``report.available`` is the single judgement the front end uses to decide
  whether the download entry point exists at all.

Nothing here reads the settings object; the capability facts arrive as plain
values, so the assembly is testable without a configured deployment and the
application layer keeps no dependency on the configuration module.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from factory_agent.domain import InteractionId, InteractionRecord
from factory_agent.observability.debug_trace import llm_span_key, mes_span_key
from factory_agent.ports.session import InteractionOwner, InteractionStore
from factory_agent.ports.trace import (
    LlmSpanRecord,
    MesSpanRecord,
    PhaseRecord,
    TraceDebugPayload,
    TraceFactReader,
    TraceFacts,
)

SCHEMA_VERSION = "1.0"

ContentCapture = str  # "none" | "structure_only" | "full"

#: Status vocabulary of ``phases[]`` / ``spans[]`` (§3.2). The metering tables
#: record ``completed`` / ``failed``; the trace view speaks the renderer's
#: vocabulary so a view never has to translate two enums at once.
_STATUS_MAP: dict[str, str] = {
    "completed": "ok",
    "ok": "ok",
    "failed": "error",
    "error": "error",
    "timeout": "timeout",
    "skipped": "skipped",
}

#: Phase identifiers are prefixed so a span's ``parent_span_id`` can never be
#: confused with a span id, and so the reference resolves to a row the consumer
#: already holds.
_PHASE_ID_PREFIX = "ph_"

#: Terminal states close the run rather than describe work in it. They still get
#: a row — an interaction that failed has phases worth seeing — but the label
#: makes the empty window self-explanatory instead of looking like a bug.
_TERMINAL_STATES = frozenset({"answered", "archived", "cancelled", "failed"})


@dataclass(frozen=True, slots=True)
class TraceCapability:
    """The compliance facts a response must declare (§4.5, §4.6)."""

    environment: str
    report_available: bool
    reason: str | None
    content_capture: ContentCapture
    #: Effective per-payload capture caps, surfaced so a renderer states the
    #: real ceiling instead of a hardcoded guess. ``None`` = capture off, so
    #: there is no ceiling to state (the banner never renders anyway).
    capture_max_payload_bytes: int | None = None
    capture_max_rows: int | None = None

    @property
    def payloads_possible(self) -> bool:
        return self.content_capture != "none"


class TraceService:
    """Reads one interaction's trace, or ``None`` when it is not the caller's.

    The ownership check comes first and is the *only* way in: facts are loaded
    by interaction id after that, never by id alone. A missing interaction and
    a foreign one are the same answer, matching the export path's
    indistinguishable-404 rule.
    """

    def __init__(
        self,
        interactions: InteractionStore,
        facts: TraceFactReader,
        capability: TraceCapability,
    ) -> None:
        self._interactions = interactions
        self._facts = facts
        self._capability = capability

    @property
    def capability(self) -> TraceCapability:
        return self._capability

    async def build(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        record = await self._interactions.get_interaction(owner, interaction_id)
        if record is None:
            return None
        facts = await self._facts.load(
            tenant_id=str(owner.tenant_id),
            user_id=str(owner.user_id),
            interaction_id=str(record.interaction_id),
        )
        return assemble_trace(
            record,
            facts,
            capability=self._capability,
            request_id=request_id,
        )


def assemble_trace(
    record: InteractionRecord,
    facts: TraceFacts,
    *,
    capability: TraceCapability,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the response document described in the front-end contract."""
    spans = _build_spans(record, facts, capability)
    phases = _build_phases(record, facts.phases, facts, spans)
    total_ms = _total_ms(record, facts, spans)
    return {
        "schema_version": SCHEMA_VERSION,
        "interaction_id": str(record.interaction_id),
        "request_id": request_id,
        "session_id": str(record.session_id),
        "capability_id": str(record.capability_id) if record.capability_id else None,
        "status": record.status.value,
        "environment": capability.environment,
        "content_capture": capability.content_capture,
        "report": _report_block(record, capability),
        "started_at": _isoformat(record.created_at),
        "total_ms": total_ms,
        #: Additive to the front-end contract: the three-segment ledger, so a
        #: renderer draws the split from one field instead of re-summing spans.
        #: ``total_ms`` above is the axis length, which may exceed the ledger
        #: when a span outlives the recorded total — the two are deliberately
        #: not the same number, and neither is derived from the other.
        "ledger": duration_breakdown(facts),
        #: Additive: the deployment's actual capture caps, so the report's
        #: truncation banner states the real ceiling, not a hardcoded one.
        "capture_limits": (
            {
                "max_payload_bytes": capability.capture_max_payload_bytes,
                "max_rows": capability.capture_max_rows,
            }
            if capability.payloads_possible
            else None
        ),
        "phases": phases,
        "spans": spans,
    }


def _report_block(record: InteractionRecord, capability: TraceCapability) -> dict[str, Any]:
    """The download entry point's judgement call.

    ``url`` is always the canonical path — it is a description of the route, not
    a claim that the route is mounted. The front end gates on ``available``.
    """
    return {
        "available": capability.report_available,
        "reason": capability.reason,
        "url": f"/v1/interactions/{record.interaction_id}/trace.html",
        "content_capture": capability.content_capture,
    }


def _build_spans(
    record: InteractionRecord, facts: TraceFacts, capability: TraceCapability
) -> list[dict[str, Any]]:
    anchor = record.created_at
    spans: list[dict[str, Any]] = []
    for index, span in enumerate(facts.mes_spans):
        spans.append(
            _mes_span(
                span,
                index=index,
                anchor=anchor,
                payloads=facts.debug_payloads,
                payloads_possible=capability.payloads_possible,
            )
        )
    for span in facts.llm_spans:
        spans.append(
            _llm_span(
                span,
                anchor=anchor,
                payloads=facts.debug_payloads,
                payloads_possible=capability.payloads_possible,
            )
        )
    # A single ordering key for both kinds: the bar starts at its offset, and
    # ties break on the derived id so the array is stable across identical
    # reads (a consumer diffing two responses must not see phantom churn).
    spans.sort(key=lambda item: (item["offset_ms"], item["span_id"]))
    return spans


def _mes_span(
    span: MesSpanRecord,
    *,
    index: int,
    anchor: datetime,
    payloads: Mapping[str, TraceDebugPayload],
    payloads_possible: bool,
) -> dict[str, Any]:
    started = span.started_at or span.occurred_at
    span_id = (
        mes_span_key(span.operation_id, span.started_at)
        if span.started_at is not None
        else f"mes:{span.operation_id}:{index}"
    )
    return {
        "span_id": span_id,
        "parent_span_id": span.parent_span_id,
        "kind": "mes",
        "operation_id": span.operation_id,
        "offset_ms": _offset_ms(started, anchor),
        "duration_ms": max(0, span.duration_ms),
        "status": _status(span.status),
        "error_category": span.error_category,
        "page_count": span.page_count,
        "row_count_bucket": span.row_count_bucket,
        "payload": _payload_block(payloads, span_id, possible=payloads_possible),
    }


def _llm_span(
    span: LlmSpanRecord,
    *,
    anchor: datetime,
    payloads: Mapping[str, TraceDebugPayload],
    payloads_possible: bool,
) -> dict[str, Any]:
    started = span.started_at or span.occurred_at
    span_id = llm_span_key(span.logical_call_id)
    return {
        "span_id": span_id,
        "parent_span_id": span.parent_span_id,
        "kind": "llm",
        "stage": span.stage,
        "logical_call_id": span.logical_call_id,
        "model_alias": span.model_alias,
        "actual_model": span.actual_model,
        "attempt": max(1, span.attempt),
        "fallback_reason": span.fallback_reason,
        "tokens": {
            "prompt": span.prompt_tokens,
            "completion": span.completion_tokens,
            "cached": span.cached_tokens,
            "reasoning": span.reasoning_tokens,
        },
        "offset_ms": _offset_ms(started, anchor),
        "duration_ms": max(0, span.duration_ms),
        "status": _status(span.status),
        "error_category": span.error_category,
        "payload": _payload_block(payloads, span_id, possible=payloads_possible),
    }


def _payload_block(
    payloads: Mapping[str, TraceDebugPayload],
    span_id: str,
    *,
    possible: bool,
) -> dict[str, Any] | None:
    """The span's content section, shaped by ``content_capture``.

    When capture is off the key is ``None`` for every span regardless of what a
    stray row might contain, so the response cannot widen because of leftover
    data. When capture is on the key is an object for *every* span, with
    ``available=false`` where nothing was stored — the consumer reads one
    declaration instead of inferring absence from a null.
    """
    if not possible:
        return None
    payload = payloads.get(span_id)
    if payload is None:
        return {
            "available": False,
            "input": None,
            "output": None,
            "truncated": False,
            "original_rows": None,
            "original_bytes": None,
        }
    return {
        "available": True,
        "input": payload.input,
        "output": payload.output,
        "truncated": payload.truncated,
        "original_rows": payload.original_rows,
        "original_bytes": payload.original_bytes,
    }


def _build_phases(
    record: InteractionRecord,
    rows: Sequence[PhaseRecord],
    facts: TraceFacts,
    spans: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse the announced states into the stage summary bar.

    Two passes are needed: the offsets first, then each window's length, which
    is the distance to the next window. Repeated announcements of the same state
    (``interaction.progress`` shares its state with the transition that opened
    the stage) collapse; a genuine re-entry — a clarification round that parses
    again — does not, because the state actually differs from its predecessor.
    """
    anchor = record.created_at
    collapsed: list[tuple[str, str, str, int]] = []
    previous: str | None = None
    for row in rows:
        if row.name == previous:
            continue
        previous = row.name
        collapsed.append((row.name, row.label, row.status, _offset_ms(row.occurred_at, anchor)))
    if not collapsed:
        return []
    total_ms = _total_ms(record, facts, spans)
    phases: list[dict[str, Any]] = []
    for index, (name, label, status, offset) in enumerate(collapsed):
        end = collapsed[index + 1][3] if index + 1 < len(collapsed) else total_ms
        phases.append(
            {
                "id": f"{_PHASE_ID_PREFIX}{name}",
                "name": name,
                "label": label,
                "offset_ms": offset,
                "duration_ms": max(0, end - offset),
                "status": _status(status),
                #: ``running`` for the stage that had not closed when the run
                #: ended; the renderer needs it to draw an open-ended bar rather
                #: than a bar of zero length.
                "terminal": name in _TERMINAL_STATES,
            }
        )
    return phases


def _total_ms(
    record: InteractionRecord, facts: TraceFacts, spans: Sequence[Mapping[str, Any]]
) -> int:
    """The timeline's length: the measured total, extended to cover its spans.

    ``interaction_fact.duration_ms`` is the measured ledger and wins. It is then
    raised to the furthest span end so a bar can never be drawn past the axis:
    a span that outlives the recorded total (a completion event dated before the
    last call returned) would otherwise overflow, and an overflowing axis
    silently misplaces every other bar.
    """
    ledger = facts.interaction_fact
    wall_clock = _wall_clock_ms(record)
    total = max(_non_negative(ledger.duration_ms if ledger else None), wall_clock, 0)
    for span in spans:
        end = int(span["offset_ms"]) + int(span["duration_ms"])
        total = max(total, end)
    return total


def _wall_clock_ms(record: InteractionRecord) -> int:
    end = record.completed_at or record.updated_at
    return max(0, int((end - record.created_at).total_seconds() * 1000))


def _offset_ms(moment: datetime, anchor: datetime) -> int:
    """Milliseconds from the interaction's start, clamped at zero.

    Negative offsets are real (a call whose derived start precedes the recorded
    creation instant by a hair, or a clock step) and are unrepresentable on the
    axis, so they clamp rather than shift the whole timeline.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0, int((moment - anchor).total_seconds() * 1000))


def _status(raw: str) -> str:
    return _STATUS_MAP.get(raw, raw)


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _non_negative(value: int | None) -> int:
    return max(0, value) if value is not None else 0


def duration_breakdown(facts: TraceFacts) -> dict[str, int]:
    """The three-segment ledger, without the span list.

    The report header renders this directly, so the arithmetic lives in one
    place rather than being repeated by each renderer. All figures are whole
    milliseconds, so two reads of the same row agree exactly.

    MES and model time are the sums of the individual call spans — the same
    durations the waterfall draws — so the header and the call list can never
    disagree. What used to be seeded here was the *capability-run wall clock*,
    which silently absorbed everything between calls (row validation, sandbox
    inserts, queueing) and made "MES 取数" dwarf the visible MES bars. The
    residual, 本地其他, is the wall clock left over once the calls are paid;
    it includes queueing and waiting, so it is not a pure "local computation"
    figure. Without spans the fact's own segments pass through unchanged.
    """
    ledger = facts.interaction_fact
    total_ms = _non_negative(ledger.duration_ms if ledger else None)
    if facts.mes_spans or facts.llm_spans:
        mes_ms = sum(span.duration_ms for span in facts.mes_spans)
        llm_ms = sum(span.duration_ms for span in facts.llm_spans)
        local_ms = max(0, total_ms - mes_ms - llm_ms)
    else:
        mes_ms = _non_negative(ledger.mes_duration_ms if ledger else None)
        llm_ms = _non_negative(ledger.llm_duration_ms if ledger else None)
        local_ms = _non_negative(ledger.local_duration_ms if ledger else None)
    return {
        "total_ms": total_ms,
        "mes_ms": mes_ms,
        "llm_ms": llm_ms,
        "local_ms": local_ms,
    }


__all__ = [
    "SCHEMA_VERSION",
    "ContentCapture",
    "TraceCapability",
    "TraceService",
    "assemble_trace",
    "duration_breakdown",
]
