"""Session persistence, capability execution, and direct metering contracts.

Every durable read and write is keyed by a trusted ``InteractionOwner`` derived
from the resolved ``TenantContext``. Callers can never supply their own tenant
or user filter, and an interaction owned by another user is indistinguishable
from one that does not exist.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from factory_agent.domain import (
    CapabilityId,
    ConversationRecord,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    MessageRecord,
    NarrowedFilters,
    Role,
    SessionEvent,
    SessionId,
    TenantId,
    TimeRange,
    UserId,
)


@dataclass(frozen=True, slots=True)
class InteractionOwner:
    """Trusted ownership pair; the only accepted durable query filter."""

    tenant_id: TenantId
    user_id: UserId


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One metering event written directly into the owning service's tables.

    The payload is a whitelisted archive-payload format (``application/usage.py``)
    and contains no prompts, detail rows, or scope ID lists. ``event_id`` is the
    idempotency key: the ``usage_event`` table uses it (with the partition
    column) as its primary key and writes are ``ON CONFLICT DO NOTHING``, so a
    repeated event is recorded exactly once.
    """

    event_id: str
    event_type: str
    tenant_id: TenantId
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InteractionCommit:
    """Atomic unit: interaction state, messages, SSE events, and usage events.

    Usage events are handed to the owning service's metering store, which writes
    them in a separate transaction after the business commit; a metering failure
    is caught and alerted without rolling back or blocking the answer.

    ``lifecycle`` is ``False`` for informational commits (progress events): they
    append their events and advance the run's ``last_event_sequence`` and
    ``updated_at``, but never rewrite the interaction's lifecycle columns. Such
    a commit can therefore never resurrect a run another process already
    cancelled — it hands the store an unchanged in-memory record, and that
    record's stale ``running`` status must not win over the durable terminal.
    """

    interaction: InteractionRecord
    messages: tuple[MessageRecord, ...] = ()
    events: tuple[SessionEvent, ...] = ()
    usage_events: tuple[UsageEvent, ...] = ()
    lifecycle: bool = True


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: tuple[MessageRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class InteractionPage:
    items: tuple[InteractionRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """One conversation row plus the aggregates the history panel renders.

    ``title_source`` is the raw text of the conversation's earliest user
    question — the store hands over the source, and the caller owns the display
    policy (truncation), so no presentation rule leaks into persistence.
    ``last_message`` and ``last_status`` describe the newest activity and are
    ``None`` for a conversation that has never been used.
    """

    conversation: ConversationRecord
    interaction_count: int
    message_count: int
    last_message: MessageRecord | None
    last_status: InteractionStatus | None
    title_source: str | None


@dataclass(frozen=True, slots=True)
class ConversationPage:
    items: tuple[ConversationSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationCreation:
    """Outcome of an idempotent create: the summary plus whether it was new.

    ``created`` is ``False`` when the conversation already existed, which the
    API edge turns into ``200`` instead of ``201``.
    """

    summary: ConversationSummary
    created: bool


@dataclass(frozen=True, slots=True)
class MesCallRecord:
    """One completed (or failed) MES HTTP call, recorded at the adapter exit.

    ``page_count`` is the page number of this request within its paged fetch
    (1 for non-paged calls); it is a supporting metric and never re-counted
    into the call count (D6). No URL, business parameter value, or credential
    ever enters this record.
    """

    operation_id: str
    page_count: int
    row_count: int
    duration_ms: int
    status: Literal["completed", "failed"]
    error_category: str | None = None
    #: Wall-clock edges of the attempt. The adapter already reads its clock at
    #: both ends, so handing them over costs nothing and gives the trace view an
    #: offset to draw; no business value travels with them.
    started_at: datetime | None = None
    ended_at: datetime | None = None


class MesCallRecorder(Protocol):
    """Records a MES call at the single ``_send`` exit point.

    The recorder is invoked synchronously after every MES HTTP attempt
    (success and failure); implementations must never raise into the adapter.
    """

    def record(self, call: MesCallRecord) -> None: ...


class InteractionStore(Protocol):
    async def commit(self, commit: InteractionCommit) -> None: ...

    async def claim_run(
        self, owner: InteractionOwner, interaction_id: InteractionId, now: datetime
    ) -> InteractionRecord | None:
        """Atomically move ``PENDING`` to ``RUNNING`` for exactly one caller.

        Returns the claimed record, or ``None`` when another connection already
        owns the run. This compare-and-set is what stops a resumed SSE
        connection from repeating a business-data call.
        """
        ...

    async def fail_stale_run(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        *,
        stale_before: datetime,
        now: datetime,
        category: str,
    ) -> InteractionRecord | None:
        """Atomically fail a ``RUNNING`` interaction whose updates went stale.

        Returns the failed record (with the terminal event sequence already
        reserved in ``last_event_sequence``), or ``None`` when the interaction
        is not running or still fresh. This compare-and-set is what lets a
        reconnecting stream recover an interaction whose executor connection
        died without persisting a terminal event.
        """
        ...

    async def fail_stale_runs(
        self,
        *,
        stale_before: datetime,
        now: datetime,
        category: str,
    ) -> tuple[InteractionRecord, ...]:
        """Bulk startup variant of ``fail_stale_run``.

        Fails every stale ``RUNNING`` interaction across all owners in one
        compare-and-set; called once at application startup. Returns the
        failed records (terminal event sequence already reserved) so the
        caller can persist each terminal event. Idempotent under repeated and
        concurrent worker starts.
        """
        ...

    async def fail_abandoned_runs(
        self,
        *,
        abandoned_before: datetime,
        now: datetime,
        category: str,
    ) -> tuple[InteractionRecord, ...]:
        """Bulk recovery: fail every ``pending`` interaction that never started.

        A ``pending`` row was persisted by ``start`` but no stream ever claimed
        it, so it can never produce an answer and no stream-driven recovery
        would ever terminate it. Rows older than the abandonment threshold are
        failed in one compare-and-set, returning the failed records with their
        terminal event sequence already reserved so the caller can persist each
        terminal event. Idempotent: only rows still ``pending`` are updated,
        each exactly once.
        """
        ...

    async def get_interaction(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord | None: ...

    async def list_events(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        after_sequence: int,
    ) -> tuple[SessionEvent, ...]: ...

    async def list_messages(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
        exclude_kinds: frozenset[MessageKind] = frozenset(),
    ) -> MessagePage: ...

    async def latest_message(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        *,
        kinds: frozenset[MessageKind],
    ) -> MessageRecord | None:
        """Newest session message whose kind is in ``kinds``, or ``None``.

        A single-row, ownership-scoped read used for conversational
        continuity — ``list_messages`` pages forward from the oldest row, so
        the newest match of a long session is not reachable in one call.
        Implementations must never widen the ownership filter.
        """
        ...

    async def list_interactions(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
    ) -> InteractionPage: ...

    async def list_conversations(
        self,
        owner: InteractionOwner,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        """One recency-ordered page of an owner's conversations.

        Newest activity first (``updated_at`` descending). A conversation that
        was created but never used is part of the page — that is the whole point
        of making it a durable row.
        """
        ...

    async def get_conversation(
        self, owner: InteractionOwner, session_id: SessionId
    ) -> ConversationSummary | None:
        """One owned conversation with its aggregates, or ``None``.

        A conversation owned by another identity must be indistinguishable from
        a missing one; implementations must never widen the ownership filter.
        """
        ...

    async def count_conversations(self, owner: InteractionOwner) -> int:
        """How many conversations this owner has, used to enforce the cap."""
        ...

    async def create_conversation(
        self, owner: InteractionOwner, session_id: SessionId, now: datetime
    ) -> ConversationCreation:
        """Create the conversation unless it already exists (idempotent).

        Repeated calls, including concurrent ones, must leave exactly one row
        and never clear existing messages.
        """
        ...

    async def delete_session(self, owner: InteractionOwner, session_id: SessionId) -> bool: ...


@dataclass(frozen=True, slots=True)
class CapabilityRunRequest:
    """Everything the bounded executor needs; scope IDs arrive only via filters.

    ``role`` is the authoritative token role; it never broadens a scope.
    """

    capability_id: CapabilityId
    filters: NarrowedFilters
    time_range: TimeRange
    role: Role | None = None


@dataclass(frozen=True, slots=True)
class CapabilityRunResult:
    capability_id: CapabilityId
    column_names: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    totals: dict[str, Decimal] = field(default_factory=lambda: {})
    source_operations: tuple[str, ...] = ()
    incomplete: bool = False
    incomplete_reason: str | None = None
    api_call_count: int = 0
    duration_ms: int = 0
    #: Render metadata so the card and Excel renderers honour types/units and
    #: assumptions without re-querying the MES or database.
    column_types: dict[str, str] | None = None
    column_units: dict[str, str] | None = None
    #: Worker-facing Chinese display labels keyed by column name.
    column_titles: dict[str, str] | None = None
    warnings: tuple[str, ...] = ()
    #: Ownership fields observed on the fetched business rows (role-consistency
    #: safety net). Distinct work numbers and dept ids actually
    #: returned by the customer MES before local compute collapses rows; never
    #: rendered, exported, logged, or persisted — consumed in memory only by
    #: the consistency validator. Empty for fakes that do not populate them.
    observed_uid_values: tuple[str, ...] = ()
    observed_dept_values: tuple[str, ...] = ()
    #: Front-end card payload (JSON-serializable) built by the kernel when the
    #: recipe declares a ``card:`` block; ``None`` = no card (old contract).
    #: The pipeline places this same dict on the ``interaction.result`` event
    #: and the persisted ``result_table`` message so live, replay and history
    #: streams carry an identical payload.
    card: dict[str, object] | None = None


class CapabilityRunner(Protocol):
    """Bounded executor seen from the application layer."""

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult: ...


__all__ = [
    "CapabilityRunRequest",
    "CapabilityRunResult",
    "CapabilityRunner",
    "ConversationCreation",
    "ConversationPage",
    "ConversationSummary",
    "InteractionCommit",
    "InteractionOwner",
    "InteractionPage",
    "InteractionStore",
    "MessagePage",
    "MesCallRecord",
    "MesCallRecorder",
    "UsageEvent",
]
