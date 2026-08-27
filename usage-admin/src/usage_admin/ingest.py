"""Idempotent batch ingest.

Rules enforced here:

- The batch is capped by event count and serialized byte size; an over-limit
  batch is rejected as a whole with a structured reason (never partially
  ingested).
- Every event is validated against the v1 contract whitelist before any write.
- ``event_id`` is the deduplication authority. A repeat with the same digest is
  an idempotent redelivery; a repeat with a different digest is a conflict that
  is rejected and alerted. Neither case touches the raw event table twice.
- Schema-unsupported events go to the restricted dead-letter table carrying
  only metadata (no raw payload).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from usage_admin.alerts import AlertSink, CollectingAlertSink
from usage_admin.events import (
    canonical_digest,
    to_interaction_fact,
    to_llm_call_fact,
    validate_event,
)
from usage_admin.store import (
    DeadLetterEntry,
    Receipt,
    UsageStore,
)

_LOGGER = logging.getLogger("usage_admin.ingest")

IngestStatus = Literal["accepted", "duplicate", "rejected"]


class IngestBatchTooLargeError(ValueError):
    """The batch exceeded the configured count or byte cap."""


@dataclass(frozen=True, slots=True)
class IngestLimits:
    max_events: int = 1000
    max_bytes: int = 1_000_000


@dataclass(frozen=True, slots=True)
class EventOutcome:
    event_id: str
    status: IngestStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IngestBatchResult:
    accepted: tuple[str, ...]
    duplicate: tuple[str, ...]
    rejected: tuple[str, ...]
    reasons: dict[str, str] = field(default_factory=dict[str, str])
    batch_reason: str | None = None

    @property
    def total(self) -> int:
        return len(self.accepted) + len(self.duplicate) + len(self.rejected)


class IngestService:
    """Applies the idempotent ingest policy against a ``UsageStore``."""

    def __init__(
        self,
        store: UsageStore,
        *,
        clock: Callable[[], datetime],
        limits: IngestLimits | None = None,
        alerts: AlertSink | None = None,
        received_at: datetime | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._limits = limits or IngestLimits()
        self._alerts = alerts or CollectingAlertSink()

    async def ingest(self, events: list[dict[str, object]]) -> IngestBatchResult:
        if not events:
            return IngestBatchResult((), (), (), {})
        if len(events) > self._limits.max_events:
            reason = f"batch exceeds {self._limits.max_events} events"
            _LOGGER.warning("usage.ingest.reject %s", reason)
            return IngestBatchResult((), (), (), {}, batch_reason=reason)
        byte_size = len(json.dumps(events, separators=(",", ":")).encode())
        if byte_size > self._limits.max_bytes:
            reason = f"batch exceeds {self._limits.max_bytes} bytes"
            _LOGGER.warning("usage.ingest.reject %s", reason)
            return IngestBatchResult((), (), (), {}, batch_reason=reason)

        now = self._clock()
        accepted: list[str] = []
        duplicate: list[str] = []
        rejected: list[str] = []
        reasons: dict[str, str] = {}
        for event in events:
            outcome = await self._ingest_one(event, now)
            bucket = (
                accepted
                if outcome.status == "accepted"
                else duplicate
                if outcome.status == "duplicate"
                else rejected
            )
            bucket.append(outcome.event_id)
            if outcome.reason is not None:
                reasons[outcome.event_id] = outcome.reason
        return IngestBatchResult(tuple(accepted), tuple(duplicate), tuple(rejected), reasons)

    async def _ingest_one(self, event: dict[str, object], now: datetime) -> EventOutcome:
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            return EventOutcome("", "rejected", "event_id must be a string")

        reason = validate_event(event)
        if reason is not None:
            await self._dead_letter(event_id, event, reason, now)
            return EventOutcome(event_id, "rejected", reason)

        digest = canonical_digest(event)
        previous = await self._store.receipt_digest(event_id)
        if previous is not None:
            if previous == digest:
                return EventOutcome(event_id, "duplicate", "idempotent redelivery")
            await self._alerts.alert(
                "ingest_digest_conflict",
                {"event_id": event_id, "reason": "same event_id, different payload digest"},
            )
            await self._dead_letter(event_id, event, "digest conflict", now)
            return EventOutcome(event_id, "rejected", "digest conflict")

        await self._store.record_receipt(
            Receipt(
                event_id=event_id,
                schema_version=str(event.get("schema_version", "")),
                event_type=str(event.get("event_type", "")),
                tenant_id=str(event.get("tenant_id", "")),
                payload_digest=digest,
                received_at=now,
            )
        )
        await self._store.insert_raw_event(event, received_at=now)
        event_type = str(event.get("event_type"))
        if event_type in ("interaction_started", "interaction_completed"):
            await self._store.insert_interaction_fact(to_interaction_fact(event, received_at=now))
        elif event_type == "llm_call_completed":
            await self._store.insert_llm_call_fact(to_llm_call_fact(event, received_at=now))
        return EventOutcome(event_id, "accepted")

    async def _dead_letter(
        self, event_id: str, event: dict[str, object], reason: str, now: datetime
    ) -> None:
        await self._store.dead_letter(
            DeadLetterEntry(
                event_id=event_id,
                event_type=str(event.get("event_type", "unknown")),
                tenant_id=str(event.get("tenant_id", "unknown")),
                payload_digest=canonical_digest(event),
                reason=reason,
                rejected_at=now,
            )
        )


__all__ = [
    "EventOutcome",
    "IngestBatchResult",
    "IngestBatchTooLargeError",
    "IngestLimits",
    "IngestService",
]
