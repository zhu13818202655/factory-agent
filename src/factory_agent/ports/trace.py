"""Trace read-model contracts (§4.6).

The trace view stitches five carriers into one timeline: the interaction row
(identity and lifecycle), ``agent_interaction_event`` (phases), and the
``llm_call_fact`` / ``mes_call_fact`` / ``interaction_fact`` metering facts,
plus the B-channel payload store when content capture is on.

This module holds only the shapes. Assembling them into the response document
is a use case (``application.trace``), and reading them out of the database is
an adapter (``persistence.trace_store``); keeping the shapes here is what lets
the assembly be tested against a fake reader instead of a database.

Every record carries the ownership pair implicitly: a caller resolves the
interaction through ``(tenant_id, user_id)`` first, and only then asks for the
facts of that interaction. There is deliberately no reader method that takes an
interaction id alone.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PhaseRecord:
    """One announced stage transition (``interaction.phase``).

    ``name`` is the ``SessionState`` value; ``label`` is the human-readable
    stage text the event already carries, so the read model does not re-derive
    display copy.
    """

    name: str
    label: str
    status: str
    sequence: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class LlmSpanRecord:
    """One ``llm_call_fact`` row: a single physical model attempt."""

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
    occurred_at: datetime
    fallback_reason: str | None = None
    error_category: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    parent_span_id: str | None = None


@dataclass(frozen=True, slots=True)
class MesSpanRecord:
    """One ``mes_call_fact`` row: a single MES HTTP attempt."""

    operation_id: str
    page_count: int
    row_count_bucket: str
    duration_ms: int
    status: str
    occurred_at: datetime
    error_category: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    parent_span_id: str | None = None


@dataclass(frozen=True, slots=True)
class InteractionFactRecord:
    """The ``interaction_completed`` fact: the three-segment duration ledger."""

    status: str | None
    duration_ms: int | None
    mes_duration_ms: int | None
    llm_duration_ms: int | None
    local_duration_ms: int | None
    result_rows_bucket: str | None = None


@dataclass(frozen=True, slots=True)
class TraceDebugPayload:
    """One captured payload, already redacted and bounded at capture time."""

    span_key: str
    input: object
    output: object
    truncated: bool
    original_rows: int | None = None
    original_bytes: int | None = None


def _no_payloads() -> dict[str, TraceDebugPayload]:
    """Empty default for the payload mapping.

    A named function rather than ``default_factory=dict`` so the mapping's
    value type survives type checking: a bare ``dict`` factory erases it to
    ``dict[Unknown, Unknown]``.
    """
    return {}


@dataclass(frozen=True, slots=True)
class TraceFacts:
    """Everything the assembly needs for one interaction, in one read."""

    phases: tuple[PhaseRecord, ...] = ()
    llm_spans: tuple[LlmSpanRecord, ...] = ()
    mes_spans: tuple[MesSpanRecord, ...] = ()
    interaction_fact: InteractionFactRecord | None = None
    #: Keyed by ``span_key``; empty when content capture is off, which is the
    #: normal case everywhere except a developer environment.
    debug_payloads: Mapping[str, TraceDebugPayload] = field(default_factory=_no_payloads)


class TraceFactReader(Protocol):
    """Loads the trace carriers of one already-owned interaction."""

    async def load(
        self,
        *,
        tenant_id: str,
        user_id: str,
        interaction_id: str,
    ) -> TraceFacts: ...


__all__ = [
    "InteractionFactRecord",
    "LlmSpanRecord",
    "MesSpanRecord",
    "PhaseRecord",
    "TraceDebugPayload",
    "TraceFactReader",
    "TraceFacts",
]
