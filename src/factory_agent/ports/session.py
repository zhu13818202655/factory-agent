"""Session persistence, capability execution, and direct metering contracts.

Every durable read and write is keyed by a trusted ``InteractionOwner`` derived
from the resolved ``TenantContext``. Callers can never supply their own tenant
or user filter, and an interaction owned by another user is indistinguishable
from one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionRecord,
    MessageRecord,
    NarrowedFilters,
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
    """

    interaction: InteractionRecord
    messages: tuple[MessageRecord, ...] = ()
    events: tuple[SessionEvent, ...] = ()
    usage_events: tuple[UsageEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: tuple[MessageRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class InteractionPage:
    items: tuple[InteractionRecord, ...]
    next_cursor: str | None


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
    ) -> MessagePage: ...

    async def list_interactions(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
    ) -> InteractionPage: ...

    async def delete_session(self, owner: InteractionOwner, session_id: SessionId) -> bool: ...


@dataclass(frozen=True, slots=True)
class CapabilityRunRequest:
    """Everything the bounded executor needs; scope IDs arrive only via filters."""

    capability_id: CapabilityId
    filters: NarrowedFilters
    time_range: TimeRange


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
    warnings: tuple[str, ...] = ()


class CapabilityRunner(Protocol):
    """Bounded executor seen from the application layer."""

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult: ...


__all__ = [
    "CapabilityRunRequest",
    "CapabilityRunResult",
    "CapabilityRunner",
    "InteractionCommit",
    "InteractionOwner",
    "InteractionPage",
    "InteractionStore",
    "MessagePage",
    "MesCallRecord",
    "MesCallRecorder",
    "UsageEvent",
]
