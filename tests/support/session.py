import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from factory_agent.domain import (
    CapabilityId,
    ConversationRecord,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    MessageRecord,
    SessionEvent,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.ports import (
    CapabilityRunRequest,
    CapabilityRunResult,
    ConversationCreation,
    ConversationPage,
    ConversationSummary,
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
    conversations: dict[str, ConversationRecord] = field(default_factory=lambda: {})
    commits: int = 0

    async def commit(self, commit: InteractionCommit) -> None:
        self.commits += 1
        record = commit.interaction
        if commit.lifecycle:
            self.interactions[str(record.interaction_id)] = record
            # Mirror the SQL store: the conversation row is created (or its
            # recency moved) in the same commit as the interaction it belongs to.
            self._refresh_conversation(record)
        else:
            # Informational commit: mirror the SQL store by advancing only the
            # bookkeeping, so an in-flight progress event cannot resurrect a
            # terminal another process persisted.
            current = self.interactions.get(str(record.interaction_id))
            if current is not None:
                self.interactions[str(record.interaction_id)] = replace(
                    current,
                    last_event_sequence=record.last_event_sequence,
                    updated_at=record.updated_at,
                )
        # Mirror the ``agent_message_sequence_key`` unique constraint so a
        # duplicate (interaction, sequence) pair fails here exactly like the
        # PostgreSQL store would.
        seen = {(m.interaction_id, m.sequence) for m in self.messages}
        for message in commit.messages:
            key = (message.interaction_id, message.sequence)
            if key in seen:
                raise ValueError(
                    f"duplicate message sequence {message.sequence} "
                    f"for interaction {message.interaction_id}"
                )
            seen.add(key)
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

    async def fail_stale_run(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        *,
        stale_before: datetime,
        now: datetime,
        category: str,
    ) -> InteractionRecord | None:
        record = await self.get_interaction(owner, interaction_id)
        if (
            record is None
            or record.status is not InteractionStatus.RUNNING
            or record.updated_at >= stale_before
        ):
            return None
        failed = replace(
            record,
            status=InteractionStatus.FAILED,
            state=SessionState.FAILED,
            error_category=category,
            updated_at=now,
            completed_at=now,
            last_event_sequence=record.last_event_sequence + 1,
        )
        self.interactions[str(interaction_id)] = failed
        return failed

    async def fail_stale_runs(
        self,
        *,
        stale_before: datetime,
        now: datetime,
        category: str,
    ) -> tuple[InteractionRecord, ...]:
        """Bulk startup sweep: fail every stale ``running`` interaction."""
        doomed = [
            key
            for key, record in self.interactions.items()
            if record.status is InteractionStatus.RUNNING and record.updated_at < stale_before
        ]
        failed: list[InteractionRecord] = []
        for key in doomed:
            record = await self.fail_stale_run(
                self._owner_of(self.interactions[key]),
                InteractionId(key),
                stale_before=stale_before,
                now=now,
                category=category,
            )
            if record is not None:
                failed.append(record)
        return tuple(failed)

    async def fail_abandoned_runs(
        self,
        *,
        abandoned_before: datetime,
        now: datetime,
        category: str,
    ) -> tuple[InteractionRecord, ...]:
        """Bulk recovery: fail every ``pending`` interaction that never ran."""
        doomed = [
            key
            for key, record in self.interactions.items()
            if record.status is InteractionStatus.PENDING and record.created_at < abandoned_before
        ]
        failed: list[InteractionRecord] = []
        for key in doomed:
            record = self.interactions[key]
            reaped = replace(
                record,
                status=InteractionStatus.FAILED,
                state=SessionState.FAILED,
                error_category=category,
                updated_at=now,
                completed_at=now,
                last_event_sequence=record.last_event_sequence + 1,
            )
            self.interactions[key] = reaped
            failed.append(reaped)
        return tuple(failed)

    @staticmethod
    def _owner_of(record: InteractionRecord) -> InteractionOwner:
        return InteractionOwner(tenant_id=record.tenant_id, user_id=record.user_id)

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
        exclude_kinds: frozenset[MessageKind] = frozenset(),
    ) -> MessagePage:
        owned = [
            message
            for message in sorted(self.messages, key=lambda item: str(item.message_id))
            if message.session_id == session_id
            and message.kind not in exclude_kinds
            and self._owns(owner, message.tenant_id, message.user_id)
        ]
        start = int(cursor) if cursor else 0
        page = owned[start : start + limit]
        next_cursor = str(start + limit) if len(owned) > start + limit else None
        return MessagePage(items=tuple(page), next_cursor=next_cursor)

    async def latest_message(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        *,
        kinds: frozenset[MessageKind],
    ) -> MessageRecord | None:
        if not kinds:
            return None
        candidates = [
            message
            for message in self.messages
            if message.session_id == session_id
            and message.kind in kinds
            and self._owns(owner, message.tenant_id, message.user_id)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.created_at, str(item.message_id)))

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

    async def list_conversations(
        self,
        owner: InteractionOwner,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        owned = [
            record
            for record in sorted(
                self.conversations.values(),
                key=lambda item: (item.updated_at, str(item.session_id)),
                reverse=True,
            )
            if self._owns(owner, record.tenant_id, record.user_id)
        ]
        start = int(cursor) if cursor else 0
        page = owned[start : start + limit]
        next_cursor = str(start + limit) if len(owned) > start + limit else None
        summaries = tuple(self._summarise(record) for record in page)
        return ConversationPage(items=summaries, next_cursor=next_cursor)

    async def get_conversation(
        self, owner: InteractionOwner, session_id: SessionId
    ) -> ConversationSummary | None:
        record = self.conversations.get(str(session_id))
        if record is None or not self._owns(owner, record.tenant_id, record.user_id):
            return None
        return self._summarise(record)

    async def count_conversations(self, owner: InteractionOwner) -> int:
        return sum(
            1
            for record in self.conversations.values()
            if self._owns(owner, record.tenant_id, record.user_id)
        )

    async def create_conversation(
        self, owner: InteractionOwner, session_id: SessionId, now: datetime
    ) -> ConversationCreation:
        existing = await self.get_conversation(owner, session_id)
        if existing is not None:
            return ConversationCreation(summary=existing, created=False)
        record = ConversationRecord(
            session_id=session_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            created_at=now,
            updated_at=now,
        )
        self.conversations[str(session_id)] = record
        return ConversationCreation(summary=self._summarise(record), created=True)

    def _refresh_conversation(self, record: InteractionRecord) -> None:
        key = str(record.session_id)
        existing = self.conversations.get(key)
        if existing is None:
            self.conversations[key] = ConversationRecord(
                session_id=record.session_id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            return
        if record.updated_at > existing.updated_at:
            self.conversations[key] = replace(existing, updated_at=record.updated_at)

    def _summarise(self, record: ConversationRecord) -> ConversationSummary:
        session_id = record.session_id
        readable = [
            message
            for message in self.messages
            if message.session_id == session_id and message.kind is not MessageKind.PHASE
        ]
        turns = [turn for turn in self.interactions.values() if turn.session_id == session_id]
        questions = [
            message
            for message in self.messages
            if message.session_id == session_id
            and message.role.value == "user"
            and message.kind is MessageKind.PLAIN_TEXT
        ]
        newest_message = max(
            readable, key=lambda item: (item.created_at, str(item.message_id)), default=None
        )
        newest_turn = max(
            turns, key=lambda item: (item.created_at, str(item.interaction_id)), default=None
        )
        earliest_question = min(
            questions, key=lambda item: (item.created_at, str(item.message_id)), default=None
        )
        return ConversationSummary(
            conversation=record,
            interaction_count=len(turns),
            message_count=len(readable),
            last_message=newest_message,
            last_status=newest_turn.status if newest_turn is not None else None,
            title_source=earliest_question.text if earliest_question is not None else None,
        )

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
    #: Optional card payload the fake kernel result carries (card-contract tests).
    card: dict[str, object] | None = None
    #: Opaque parking spot for a recipe registry. Nothing reads it — the session
    #: service never consults the runner's registry — but a drill test documents
    #: which capabilities the round could route by setting it.
    recipes: object | None = None

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
            card=self.card,
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
    counter: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def __call__(self) -> str:
        return f"{self.prefix}-{next(self.counter)}"


@dataclass(frozen=True)
class FrozenClock:
    current: datetime = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current


@dataclass
class TickingClock:
    """Clock that advances one millisecond per read, like a real wall clock.

    Needed wherever a duration is asserted: a frozen clock makes every derived
    total exactly zero, which hides the arithmetic under test.
    """

    current: datetime = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
    step: timedelta = timedelta(milliseconds=1)

    def now(self) -> datetime:
        self.current = self.current + self.step
        return self.current


def unavailable_gateway_error() -> ModelGatewayError:
    from factory_agent.ports import ModelErrorCategory

    return ModelGatewayError(ModelErrorCategory.UNAVAILABLE, "gateway request failed")


def capability_id(value: str) -> CapabilityId:
    return CapabilityId(value)
