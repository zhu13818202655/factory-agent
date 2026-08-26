"""Session persistence, capability execution, and usage outbox contracts.

Every durable read and write is keyed by a trusted ``InteractionOwner`` derived
from the resolved ``TenantContext``. Callers can never supply their own tenant
or user filter, and an interaction owned by another user is indistinguishable
from one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

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
class UsageOutboxEvent:
    """One versioned usage event awaiting publication.

    ``payload`` is validated against ``contracts/usage-events/v1`` before it is
    enqueued and contains no prompts, detail rows, or scope ID lists.
    """

    event_id: str
    event_type: str
    tenant_id: TenantId
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InteractionCommit:
    """Atomic unit: interaction state, messages, SSE events, and usage events.

    A usage-admin outage can never change the answer outcome because publication
    happens outside this transaction.
    """

    interaction: InteractionRecord
    messages: tuple[MessageRecord, ...] = ()
    events: tuple[SessionEvent, ...] = ()
    usage_events: tuple[UsageOutboxEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: tuple[MessageRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class InteractionPage:
    items: tuple[InteractionRecord, ...]
    next_cursor: str | None


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
    """Story 3 bounded executor seen from the application layer."""

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult: ...


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    event_id: str
    event_type: str
    tenant_id: TenantId
    payload: dict[str, object]
    attempts: int
    available_at: datetime


class UsageOutbox(Protocol):
    async def claim(self, limit: int, now: datetime) -> tuple[OutboxRecord, ...]: ...

    async def mark_published(self, event_ids: tuple[str, ...], now: datetime) -> None: ...

    async def mark_failed(
        self,
        event_ids: tuple[str, ...],
        reason: str,
        retry_at: datetime,
        dead_letter: bool,
    ) -> None: ...

    async def backlog_size(self, now: datetime) -> int: ...


class UsageEventSink(Protocol):
    """Batch transport to usage-admin; returns the accepted event IDs."""

    async def publish(self, records: tuple[OutboxRecord, ...]) -> tuple[str, ...]: ...


__all__ = [
    "CapabilityRunRequest",
    "CapabilityRunResult",
    "CapabilityRunner",
    "InteractionCommit",
    "InteractionOwner",
    "InteractionPage",
    "InteractionStore",
    "MessagePage",
    "OutboxRecord",
    "UsageEventSink",
    "UsageOutbox",
    "UsageOutboxEvent",
]
