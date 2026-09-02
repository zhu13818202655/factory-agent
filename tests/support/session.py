from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal

from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageRecord,
    SessionEvent,
    SessionId,
    TenantId,
    UserId,
)
from factory_agent.ports import (
    CapabilityRunRequest,
    CapabilityRunResult,
    InteractionCommit,
    InteractionOwner,
    InteractionPage,
    MessagePage,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    UsageEvent,
)


@dataclass
class InMemoryInteractionStore:
    """Ownership-filtered in-memory store used by offline session tests."""

    interactions: dict[str, InteractionRecord] = field(default_factory=lambda: {})
    messages: list[MessageRecord] = field(default_factory=lambda: [])
    events: dict[str, list[SessionEvent]] = field(default_factory=lambda: {})
    usage_events: list[UsageEvent] = field(default_factory=lambda: [])
    commits: int = 0

    async def commit(self, commit: InteractionCommit) -> None:
        self.commits += 1
        record = commit.interaction
        self.interactions[str(record.interaction_id)] = record
        self.messages.extend(commit.messages)
        stored = self.events.setdefault(str(record.interaction_id), [])
        known = {event.sequence for event in stored}
        stored.extend(event for event in commit.events if event.sequence not in known)
        stored.sort(key=lambda event: event.sequence)
        self.usage_events.extend(commit.usage_events)

    async def get_interaction(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord | None:
        record = self.interactions.get(str(interaction_id))
        if record is None or not self._owns(owner, record.tenant_id, record.user_id):
            return None
        return record

    async def claim_run(
        self, owner: InteractionOwner, interaction_id: InteractionId, now: datetime
    ) -> InteractionRecord | None:
        record = await self.get_interaction(owner, interaction_id)
        if record is None or record.status is not InteractionStatus.PENDING:
            return None
        claimed = replace(record, status=InteractionStatus.RUNNING, updated_at=now)
        self.interactions[str(interaction_id)] = claimed
        return claimed

    async def list_events(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        after_sequence: int,
    ) -> tuple[SessionEvent, ...]:
        record = await self.get_interaction(owner, interaction_id)
        if record is None:
            return ()
        return tuple(
            event
            for event in self.events.get(str(interaction_id), [])
            if event.sequence > after_sequence
        )

    async def list_messages(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
    ) -> MessagePage:
        owned = [
            message
            for message in sorted(self.messages, key=lambda item: str(item.message_id))
            if message.session_id == session_id
            and self._owns(owner, message.tenant_id, message.user_id)
        ]
        start = int(cursor) if cursor else 0
        page = owned[start : start + limit]
        next_cursor = str(start + limit) if len(owned) > start + limit else None
        return MessagePage(items=tuple(page), next_cursor=next_cursor)

    async def list_interactions(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        limit: int,
        cursor: str | None = None,
    ) -> InteractionPage:
        owned = [
            record
            for record in sorted(self.interactions.values(), key=lambda item: item.created_at)
            if record.session_id == session_id
            and self._owns(owner, record.tenant_id, record.user_id)
        ]
        start = int(cursor) if cursor else 0
        page = owned[start : start + limit]
        next_cursor = str(start + limit) if len(owned) > start + limit else None
        return InteractionPage(items=tuple(page), next_cursor=next_cursor)

    async def delete_session(self, owner: InteractionOwner, session_id: SessionId) -> bool:
        doomed = [
            key
            for key, record in self.interactions.items()
            if record.session_id == session_id
            and self._owns(owner, record.tenant_id, record.user_id)
        ]
        for key in doomed:
            self.interactions.pop(key)
            self.events.pop(key, None)
        self.messages = [
            message for message in self.messages if str(message.interaction_id) not in doomed
        ]
        return bool(doomed)

    @staticmethod
    def _owns(owner: InteractionOwner, tenant_id: TenantId, user_id: UserId) -> bool:
        return owner.tenant_id == tenant_id and owner.user_id == user_id


@dataclass
class RecordingCapabilityRunner:
    """Counts business-data executions so denial paths can assert zero calls."""

    rows: tuple[tuple[object, ...], ...] = ((1, Decimal("2")),)
    column_names: tuple[str, ...] = ("qualified_quantity_total", "amount_total")
    requests: list[CapabilityRunRequest] = field(default_factory=lambda: [])
    failure: Exception | None = None

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return CapabilityRunResult(
            capability_id=request.capability_id,
            column_names=self.column_names,
            rows=self.rows,
            totals={"amount_total": Decimal("2")},
            source_operations=("C1_listPieceworkRecords",),
            api_call_count=1,
            duration_ms=7,
        )


@dataclass
class ScriptedModelGateway:
    """In-process fake gateway: scripted contents or scripted failures."""

    contents: list[str] = field(default_factory=lambda: [])
    failures: list[Exception | None] = field(default_factory=lambda: [])
    requests: list[ModelRequest] = field(default_factory=lambda: [])
    actual_model: str = "qwen3-32b-local"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index < len(self.failures) and self.failures[index] is not None:
            raise self.failures[index]  # pyright: ignore[reportGeneralTypeIssues]
        content = self.contents[min(index, len(self.contents) - 1)] if self.contents else "{}"
        return ModelResponse(
            content=content,
            actual_model=self.actual_model,
            usage=ModelUsage(prompt_tokens=11, completion_tokens=5),
            duration_ms=3,
        )


@dataclass
class SequentialIds:
    """Deterministic identifier factory for reproducible snapshots."""

    prefix: str = "id"
    counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def __call__(self) -> str:
        return f"{self.prefix}-{next(self.counter)}"


@dataclass(frozen=True)
class FrozenClock:
    current: datetime = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current


def unavailable_gateway_error() -> ModelGatewayError:
    from factory_agent.ports import ModelErrorCategory

    return ModelGatewayError(ModelErrorCategory.UNAVAILABLE, "gateway request failed")


def capability_id(value: str) -> CapabilityId:
    return CapabilityId(value)
