"""Bounded session orchestration.

Replaces the report-agent monolithic ``ReportService``: this module owns the
state machine, clarification budget, idempotent execution, and durable event
sequence, while identity resolution, capability execution, and model calls stay
behind ports. Authorization always completes before any business-data call, and
scope identifiers reach the executor only through ``NarrowedFilters``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace

from factory_agent.application.authorization import (
    AuthorizationService,
    ResolvedAuthorization,
)
from factory_agent.application.context import ConversationTurn
from factory_agent.application.filters import FilterNarrower, FilterRejectionError
from factory_agent.application.intent import CapabilityIntentParser, clarification_for
from factory_agent.application.permission_matrix import Capability, authorize_capability
from factory_agent.application.structured import StructuredOutputError
from factory_agent.application.usage import (
    UsageContext,
    completion_status,
    interaction_completed_event,
    interaction_started_event,
    llm_call_event,
    new_trace_id,
)
from factory_agent.domain import (
    INTERACTION_CLARIFICATION,
    INTERACTION_HEARTBEAT,
    INTERACTION_PHASE,
    INTERACTION_RESULT,
    INTERACTION_STARTED,
    CapabilityIntent,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageId,
    MessageKind,
    MessageRecord,
    MessageRole,
    SessionEvent,
    SessionId,
    SessionState,
    SessionStateMachine,
    TimeRange,
    terminal_event_name,
)
from factory_agent.ports import (
    CapabilityRunRequest,
    CapabilityRunResult,
    Clock,
    InteractionCommit,
    InteractionOwner,
    InteractionStore,
    ModelGatewayError,
    ModelStage,
    TrustedCredential,
    UsageOutboxEvent,
)
from factory_agent.ports.session import CapabilityRunner

IdFactory = Callable[[], str]


class InteractionNotFoundError(LookupError):
    """Raised for both a missing interaction and one owned by another user."""


@dataclass(frozen=True, slots=True)
class SessionLimits:
    max_input_chars: int = 2000
    max_clarification_rounds: int = 3
    heartbeat_seconds: float = 15.0
    follow_timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class StartRequest:
    """Everything a turn needs; ownership never comes from the request body."""

    session_id: SessionId
    text: str
    history: tuple[ConversationTurn, ...] = ()
    clarification_rounds: int = 0


class SessionService:
    def __init__(
        self,
        store: InteractionStore,
        authorization: AuthorizationService,
        parser: CapabilityIntentParser,
        runner: CapabilityRunner,
        clock: Clock,
        *,
        new_id: IdFactory,
        narrower: FilterNarrower | None = None,
        limits: SessionLimits | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._authorization = authorization
        self._parser = parser
        self._runner = runner
        self._clock = clock
        self._new_id = new_id
        self._narrower = narrower or FilterNarrower()
        self._limits = limits or SessionLimits()
        self._sleep = sleep or asyncio.sleep

    async def start(
        self, credential: TrustedCredential, request: StartRequest
    ) -> InteractionRecord:
        """Persist the interaction and its first user message before streaming."""
        text = request.text.strip()
        if not text:
            raise ValueError("interaction text must not be empty")
        if len(text) > self._limits.max_input_chars:
            raise ValueError("interaction text exceeds the configured maximum length")

        now = self._clock.now()
        authorization = await self._authorization.authorize(credential, now)
        context = authorization.tenant_context
        interaction_id = InteractionId(self._new_id())
        record = InteractionRecord(
            interaction_id=interaction_id,
            session_id=request.session_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            status=InteractionStatus.PENDING,
            state=SessionState.PARSING,
            input_text=text,
            capability_id=None,
            clarification_rounds=request.clarification_rounds,
            last_event_sequence=0,
            error_category=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        usage = UsageContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=request.session_id,
            interaction_id=interaction_id,
            trace_id=new_trace_id(),
        )
        await self._store.commit(
            InteractionCommit(
                interaction=record,
                messages=(
                    self._message(record, MessageRole.USER, MessageKind.PLAIN_TEXT, 1, text),
                ),
                usage_events=(
                    interaction_started_event(
                        usage,
                        occurred_at=now,
                        capability=None,
                        entrypoint="api",
                        role=context.role,
                    ),
                ),
            )
        )
        return record

    async def stream(
        self,
        credential: TrustedCredential,
        interaction_id: InteractionId,
        *,
        after_sequence: int = 0,
        history: tuple[ConversationTurn, ...] = (),
    ) -> AsyncIterator[SessionEvent]:
        """Yield the event sequence, replaying persisted events on reconnect.

        Exactly one connection ever executes the pipeline: the ``PENDING`` to
        ``RUNNING`` claim is a durable compare-and-set, so a resumed connection
        never repeats a business-data call.
        """
        owner, authorization = await self._resolve_owner(credential)
        record = await self._load(owner, interaction_id)

        replayed = await self._store.list_events(owner, interaction_id, after_sequence)
        for event in replayed:
            yield event
        if replayed:
            after_sequence = replayed[-1].sequence
        if any(event.name in _TERMINAL_NAMES for event in replayed):
            return

        record = await self._load(owner, interaction_id)
        if record.status in _TERMINAL_STATUSES:
            async for event in self._replay_tail(owner, interaction_id, after_sequence):
                yield event
            return

        claimed = await self._store.claim_run(owner, interaction_id, self._clock.now())
        if claimed is not None:
            async for event in self._run(owner, authorization, claimed, history, after_sequence):
                yield event
            return

        async for event in self._follow(owner, interaction_id, after_sequence):
            yield event

    async def cancel(
        self, credential: TrustedCredential, interaction_id: InteractionId
    ) -> InteractionRecord:
        """Persist a cancelled terminal state and stop the remaining call budget."""
        owner, _ = await self._resolve_owner(credential)
        record = await self._load(owner, interaction_id)
        if record.status in _TERMINAL_STATUSES:
            return record
        now = self._clock.now()
        machine = SessionStateMachine(state=record.state).transition_to(
            SessionState.CANCELLED, "user_requested", now
        )
        cancelled = replace(
            record,
            status=InteractionStatus.CANCELLED,
            state=machine.state,
            updated_at=now,
            completed_at=now,
            error_category="cancelled",
            last_event_sequence=record.last_event_sequence + 1,
        )
        event = SessionEvent(
            sequence=cancelled.last_event_sequence,
            name=terminal_event_name(InteractionStatus.CANCELLED),
            data={"interaction_id": str(interaction_id), "reason": "user_requested"},
        )
        await self._store.commit(
            InteractionCommit(
                interaction=cancelled,
                messages=(
                    self._message(
                        cancelled,
                        MessageRole.SYSTEM,
                        MessageKind.ERROR,
                        cancelled.last_event_sequence + 1,
                        "已取消本次查询。",
                    ),
                ),
                events=(event,),
                usage_events=(
                    self._completion_event(cancelled, result=None, error_category="cancelled"),
                ),
            )
        )
        return cancelled

    async def _run(
        self,
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        record: InteractionRecord,
        history: tuple[ConversationTurn, ...],
        after_sequence: int,
    ) -> AsyncIterator[SessionEvent]:
        state = _RunState(record=record, sequence=max(record.last_event_sequence, after_sequence))
        started = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_STARTED,
            data={
                "interaction_id": str(record.interaction_id),
                "session_id": str(record.session_id),
                "state": SessionState.PARSING.value,
            },
        )
        state.record = replace(
            state.record,
            status=InteractionStatus.RUNNING,
            last_event_sequence=started.sequence,
            updated_at=self._clock.now(),
        )
        await self._store.commit(InteractionCommit(interaction=state.record, events=(started,)))
        yield started

        async for event in self._pipeline(owner, authorization, state, history):
            yield event

    async def _pipeline(
        self,
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        state: _RunState,
        history: tuple[ConversationTurn, ...],
    ) -> AsyncIterator[SessionEvent]:
        usage_events: list[UsageOutboxEvent] = []
        try:
            intent = await self._parse(state, history, usage_events)
        except ModelGatewayError as exc:
            async for event in self._fail(state, f"gateway_{exc.category.value}", usage_events):
                yield event
            return
        except StructuredOutputError:
            async for event in self._fail(state, "model_output_invalid", usage_events):
                yield event
            return

        if intent.needs_clarification:
            if state.record.clarification_rounds + 1 >= self._limits.max_clarification_rounds:
                async for event in self._fail(state, "clarification_exhausted", usage_events):
                    yield event
                return
            async for event in self._clarify(state, intent, usage_events):
                yield event
            return

        capability_id = intent.capability_id
        if capability_id is None:
            async for event in self._fail(state, "capability_unresolved", usage_events):
                yield event
            return

        yield await self._phase(state, SessionState.AUTHORIZING, "intent_complete")

        # Re-resolve scope after parsing so a context patch can never reuse an
        # older, broader scope.
        decision_context = authorization.tenant_context
        scope = authorization.data_scope
        try:
            capability = Capability(str(capability_id))
        except ValueError:
            async for event in self._fail(state, "capability_unregistered", usage_events):
                yield event
            return

        decision = authorize_capability(capability, decision_context, scope)
        if not decision.allowed:
            async for event in self._reject(state, "forbidden", usage_events):
                yield event
            return

        try:
            filters = self._narrower.narrow(scope)
        except FilterRejectionError as exc:
            async for event in self._reject(state, f"filter_{exc.code}", usage_events):
                yield event
            return

        time_range = _time_range(intent)
        if time_range is None:
            async for event in self._fail(state, "time_range_missing", usage_events):
                yield event
            return

        yield await self._phase(state, SessionState.EXECUTING, "authorized")

        try:
            result = await self._runner.run(
                CapabilityRunRequest(
                    capability_id=capability_id, filters=filters, time_range=time_range
                )
            )
        except Exception:
            async for event in self._fail(state, "execution_failed", usage_events):
                yield event
            return

        yield await self._phase(state, SessionState.COMPOSING, "execution_complete")

        state.record = replace(state.record, capability_id=capability_id)
        result_event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_RESULT,
            data={
                "capability_id": str(capability_id),
                "columns": list(result.column_names),
                "row_count": len(result.rows),
                "incomplete": result.incomplete,
                "incomplete_reason": result.incomplete_reason,
            },
        )
        answered = self._advance(state.record, SessionState.ANSWERED, "result_ready")
        completed = replace(
            answered,
            status=InteractionStatus.COMPLETED,
            last_event_sequence=result_event.sequence,
            updated_at=self._clock.now(),
        )
        terminal = SessionEvent(
            sequence=state.next_sequence(),
            name=terminal_event_name(InteractionStatus.COMPLETED),
            data={"interaction_id": str(state.record.interaction_id), "status": "completed"},
        )
        state.record = replace(
            completed,
            last_event_sequence=terminal.sequence,
            completed_at=self._clock.now(),
        )
        usage_events.append(
            self._completion_event(state.record, result=result, error_category=None)
        )
        await self._store.commit(
            InteractionCommit(
                interaction=state.record,
                messages=(
                    self._message(
                        state.record,
                        MessageRole.ASSISTANT,
                        MessageKind.RESULT_TABLE,
                        terminal.sequence,
                        f"已返回 {len(result.rows)} 行结果。",
                        payload={
                            "capability_id": str(capability_id),
                            "columns": list(result.column_names),
                            "row_count": len(result.rows),
                        },
                    ),
                ),
                events=(result_event, terminal),
                usage_events=tuple(usage_events),
            )
        )
        yield result_event
        yield terminal

    async def _parse(
        self,
        state: _RunState,
        history: tuple[ConversationTurn, ...],
        usage_events: list[UsageOutboxEvent],
    ) -> CapabilityIntent:
        logical_call_id = self._new_id()
        now = self._clock.now()
        try:
            outcome = await self._parser.parse(
                state.record.input_text,
                now=now,
                logical_call_id=logical_call_id,
                history=history,
            )
        except ModelGatewayError as exc:
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=now,
                    logical_call_id=logical_call_id,
                    stage=ModelStage.EXTRACT,
                    model_alias="factory-fast",
                    actual_model="unknown",
                    attempt=exc.attempt,
                    duration_ms=exc.duration_ms,
                    status="failed",
                    error_category=exc.category.value,
                )
            )
            raise
        usage_events.append(
            llm_call_event(
                self._usage_context(state.record),
                occurred_at=now,
                logical_call_id=logical_call_id,
                stage=ModelStage.EXTRACT,
                model_alias="factory-fast",
                actual_model=outcome.actual_model,
                attempt=outcome.attempts,
                duration_ms=outcome.duration_ms,
                status="completed",
            )
        )
        return outcome.intent

    async def _clarify(
        self,
        state: _RunState,
        intent: CapabilityIntent,
        usage_events: list[UsageOutboxEvent],
    ) -> AsyncIterator[SessionEvent]:
        question = clarification_for(intent) or "请补充更多信息。"
        clarifying = self._advance(state.record, SessionState.CLARIFYING, "slots_missing")
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_CLARIFICATION,
            data={
                "question": question,
                "missing": list(intent.missing),
                "ambiguous": list(intent.ambiguous),
            },
        )
        terminal = SessionEvent(
            sequence=state.next_sequence(),
            name=terminal_event_name(InteractionStatus.COMPLETED),
            data={"interaction_id": str(state.record.interaction_id), "status": "clarifying"},
        )
        now = self._clock.now()
        state.record = replace(
            clarifying,
            status=InteractionStatus.COMPLETED,
            clarification_rounds=state.record.clarification_rounds + 1,
            last_event_sequence=terminal.sequence,
            updated_at=now,
            completed_at=now,
        )
        usage_events.append(self._completion_event(state.record, result=None, error_category=None))
        await self._store.commit(
            InteractionCommit(
                interaction=state.record,
                messages=(
                    self._message(
                        state.record,
                        MessageRole.ASSISTANT,
                        MessageKind.CLARIFICATION,
                        terminal.sequence,
                        question,
                    ),
                ),
                events=(event, terminal),
                usage_events=tuple(usage_events),
            )
        )
        yield event
        yield terminal

    async def _phase(self, state: _RunState, target: SessionState, reason: str) -> SessionEvent:
        advanced = self._advance(state.record, target, reason)
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_PHASE,
            data={"state": target.value, "reason": reason},
        )
        state.record = replace(
            advanced, last_event_sequence=event.sequence, updated_at=self._clock.now()
        )
        await self._store.commit(InteractionCommit(interaction=state.record, events=(event,)))
        return event

    async def _fail(
        self, state: _RunState, category: str, usage_events: list[UsageOutboxEvent]
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._terminate(
            state, InteractionStatus.FAILED, category, usage_events, "查询未能完成。"
        ):
            yield event

    async def _reject(
        self, state: _RunState, category: str, usage_events: list[UsageOutboxEvent]
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._terminate(
            state,
            InteractionStatus.FAILED,
            category,
            usage_events,
            "当前角色无权执行该查询。",
        ):
            yield event

    async def _terminate(
        self,
        state: _RunState,
        status: InteractionStatus,
        category: str,
        usage_events: list[UsageOutboxEvent],
        text: str,
    ) -> AsyncIterator[SessionEvent]:
        now = self._clock.now()
        target = (
            SessionState.FAILED if status is InteractionStatus.FAILED else SessionState.CANCELLED
        )
        advanced = self._advance(state.record, target, category)
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=terminal_event_name(status),
            data={
                "interaction_id": str(state.record.interaction_id),
                "error_category": category,
            },
        )
        state.record = replace(
            advanced,
            status=status,
            error_category=category,
            last_event_sequence=event.sequence,
            updated_at=now,
            completed_at=now,
        )
        usage_events.append(
            self._completion_event(state.record, result=None, error_category=category)
        )
        await self._store.commit(
            InteractionCommit(
                interaction=state.record,
                messages=(
                    self._message(
                        state.record,
                        MessageRole.ASSISTANT,
                        MessageKind.ERROR,
                        event.sequence,
                        text,
                    ),
                ),
                events=(event,),
                usage_events=tuple(usage_events),
            )
        )
        yield event

    async def _replay_tail(
        self, owner: InteractionOwner, interaction_id: InteractionId, after_sequence: int
    ) -> AsyncIterator[SessionEvent]:
        for event in await self._store.list_events(owner, interaction_id, after_sequence):
            yield event

    async def _follow(
        self, owner: InteractionOwner, interaction_id: InteractionId, after_sequence: int
    ) -> AsyncIterator[SessionEvent]:
        """Tail an interaction another connection is executing; never re-run it."""
        waited = 0.0
        while waited <= self._limits.follow_timeout_seconds:
            events = await self._store.list_events(owner, interaction_id, after_sequence)
            for event in events:
                yield event
                after_sequence = event.sequence
                if event.name in _TERMINAL_NAMES:
                    return
            if not events:
                yield SessionEvent(
                    sequence=after_sequence,
                    name=INTERACTION_HEARTBEAT,
                    data={"interaction_id": str(interaction_id)},
                )
            await self._sleep(self._limits.heartbeat_seconds)
            waited += self._limits.heartbeat_seconds

    async def _resolve_owner(
        self, credential: TrustedCredential
    ) -> tuple[InteractionOwner, ResolvedAuthorization]:
        authorization = await self._authorization.authorize(credential, self._clock.now())
        context = authorization.tenant_context
        return (
            InteractionOwner(tenant_id=context.tenant_id, user_id=context.user_id),
            authorization,
        )

    async def _load(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord:
        record = await self._store.get_interaction(owner, interaction_id)
        if record is None:
            raise InteractionNotFoundError("interaction does not exist")
        return record

    def _advance(
        self, record: InteractionRecord, target: SessionState, reason: str
    ) -> InteractionRecord:
        machine = SessionStateMachine(state=record.state).transition_to(
            target, reason, self._clock.now()
        )
        return replace(record, state=machine.state)

    def _message(
        self,
        record: InteractionRecord,
        role: MessageRole,
        kind: MessageKind,
        sequence: int,
        text: str,
        payload: dict[str, object] | None = None,
    ) -> MessageRecord:
        return MessageRecord(
            message_id=MessageId(self._new_id()),
            interaction_id=record.interaction_id,
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            role=role,
            kind=kind,
            sequence=sequence,
            text=text,
            payload=payload or {},
            created_at=self._clock.now(),
        )

    def _usage_context(self, record: InteractionRecord) -> UsageContext:
        return UsageContext(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            session_id=record.session_id,
            interaction_id=record.interaction_id,
            trace_id=new_trace_id(),
        )

    def _completion_event(
        self,
        record: InteractionRecord,
        *,
        result: CapabilityRunResult | None,
        error_category: str | None,
    ) -> UsageOutboxEvent:
        duration_ms = max(0, int((record.updated_at - record.created_at).total_seconds() * 1000))
        return interaction_completed_event(
            self._usage_context(record),
            occurred_at=record.updated_at,
            status=completion_status(record.status),
            duration_ms=duration_ms,
            mes_duration_ms=result.duration_ms if result is not None else 0,
            llm_duration_ms=0,
            local_duration_ms=0,
            result_row_count=len(result.rows) if result is not None else 0,
            error_category=error_category,
        )


@dataclass
class _RunState:
    record: InteractionRecord
    sequence: int

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence


_TERMINAL_STATUSES = frozenset(
    {InteractionStatus.COMPLETED, InteractionStatus.FAILED, InteractionStatus.CANCELLED}
)
_TERMINAL_NAMES = frozenset(
    {
        terminal_event_name(InteractionStatus.COMPLETED),
        terminal_event_name(InteractionStatus.FAILED),
        terminal_event_name(InteractionStatus.CANCELLED),
    }
)


def _time_range(intent: CapabilityIntent) -> TimeRange | None:
    start = intent.slots.time_range_start
    end = intent.slots.time_range_end
    if start is None or end is None:
        return None
    return TimeRange(start=start, end=end)


__all__ = [
    "InteractionNotFoundError",
    "SessionLimits",
    "SessionService",
    "StartRequest",
]
