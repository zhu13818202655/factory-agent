"""SessionService: public API composing the session mixin layers."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

from factory_agent.application.context import ConversationTurn
from factory_agent.application.session.definitions import (
    TERMINAL_NAMES,
    TERMINAL_STATUSES,
    DrillPayload,
    StartRequest,
)
from factory_agent.application.session.history import SessionHistoryMixin
from factory_agent.application.session.lifecycle import SessionLifecycleMixin
from factory_agent.application.session.pipeline import SessionPipelineMixin
from factory_agent.application.usage import (
    UsageContext,
    interaction_started_event,
    new_trace_id,
    set_usage_context,
)
from factory_agent.domain import (
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    MessageRole,
    SessionEvent,
    SessionState,
    SessionStateMachine,
    terminal_event_name,
)
from factory_agent.observability.context import bind_interaction_id
from factory_agent.ports import InteractionCommit, TrustedCredential


class SessionService(SessionPipelineMixin, SessionHistoryMixin, SessionLifecycleMixin):
    """Thin orchestrator: ownership-checked entry points over the pipeline.

    Layering (each mixin only calls methods of the layers below it):
    SessionCore (dependencies + primitives) -> SessionOutcomeMixin (terminal /
    clarification events) -> SessionConsistencyMixin (scope guard + safety net)
    -> SessionPipelineMixin (conversation pipeline) ; SessionHistoryMixin and
    SessionLifecycleMixin hang off SessionCore directly.
    """

    async def start(
        self, credential: TrustedCredential, request: StartRequest
    ) -> InteractionRecord:
        """Persist the interaction and its first user message before streaming."""
        text = request.text.strip()
        if not text:
            raise ValueError("interaction text must not be empty")
        if len(text) > self._limits.max_input_chars:
            raise ValueError("interaction text exceeds the configured maximum length")
        if request.drill is not None:
            self._validate_drill(request.drill)

        now = self._clock.now()
        interaction_id = InteractionId(self._new_id())
        bind_interaction_id(str(interaction_id))
        # Bind the usage context before authorization so MES directory calls
        # (EmployeeQuery/DeptQuery) during scope resolution are metered too.
        usage = UsageContext(
            tenant_id=credential.tenant_id,
            user_id=credential.user_id,
            session_id=request.session_id,
            interaction_id=interaction_id,
            trace_id=new_trace_id(),
        )
        set_usage_context(usage)
        try:
            authorization = await self._authorization.authorize(credential, now)
        finally:
            set_usage_context(None)
        context = authorization.tenant_context
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
                        entrypoint="api",
                        role=context.role,
                    ),
                ),
            )
        )
        if request.drill is not None:
            self._drill_requests[str(interaction_id)] = request.drill
        return record

    def _validate_drill(self, drill: DrillPayload) -> None:
        """Structural drill validation before the interaction is persisted.

        Capability must be a registered recipe; slots are bounded so a bad
        payload is a 400 at the boundary instead of a mid-pipeline failure.
        Scope validation happens later in the pipeline against the active
        DataScope.
        """
        recipes = getattr(self._runner, "recipes", None)
        capability_id = drill.capability_id
        if not capability_id or "/" in capability_id or ".." in capability_id:
            raise ValueError("drill capability_id is not acceptable")
        if recipes is not None and capability_id not in recipes.capability_ids:
            raise ValueError("drill capability is not registered")
        if len(drill.dept_ids) > 20:
            raise ValueError("drill dept_ids exceeds the maximum count")
        if any(not value or len(value) > 64 for value in drill.dept_ids):
            raise ValueError("drill dept_ids contain an invalid value")
        if drill.employee_uid is not None and not 0 < len(drill.employee_uid) <= 64:
            raise ValueError("drill employee_uid is not acceptable")
        if drill.time_expression is not None and len(drill.time_expression) > 64:
            raise ValueError("drill time_expression is not acceptable")

    async def stream(
        self,
        credential: TrustedCredential,
        interaction_id: InteractionId,
        *,
        after_sequence: int = 0,
        history: tuple[ConversationTurn, ...] = (),
    ) -> AsyncIterator[SessionEvent]:
        """Yield the event sequence, replaying persisted events on reconnect.

        Exactly one executor ever runs the pipeline: the ``PENDING`` to
        ``RUNNING`` claim is a durable compare-and-set, so a resumed connection
        never repeats a business-data call. The claim winner hands the pipeline
        to a background executor and itself tails the persisted
        events like any other connection — the execution lifecycle is bound to
        the interaction, not to any SSE connection.
        """
        # Bound before the claim so the background executor task, created with
        # ``asyncio.create_task``, inherits this correlation id with its copy of
        # the current context and every pipeline log record carries it.
        bind_interaction_id(str(interaction_id))
        owner, authorization = await self._resolve_owner(credential)
        record = await self._load(owner, interaction_id)

        replayed = await self._store.list_events(owner, interaction_id, after_sequence)
        for event in replayed:
            yield event
        if replayed:
            after_sequence = replayed[-1].sequence
        if any(event.name in TERMINAL_NAMES for event in replayed):
            return

        record = await self._load(owner, interaction_id)
        if record.status in TERMINAL_STATUSES:
            async for event in self._replay_tail(owner, interaction_id, after_sequence):
                yield event
            return

        claimed = await self._store.claim_run(owner, interaction_id, self._clock.now())
        if claimed is not None:
            drill = self._drill_requests.pop(str(interaction_id), None)
            if not history:
                # Multi-turn context comes from this session's own terminal
                # turns unless the caller supplied an explicit history; only the
                # claiming connection rebuilds it (never a replayed connection).
                history = await self._session_history(
                    owner, claimed.session_id, exclude_interaction_id=claimed.interaction_id
                )
            self._spawn_executor(owner, authorization, claimed, history, credential, drill=drill)
            # Give the executor its first scheduling slice so the follow loop
            # below observes the freshly claimed run without an intervening
            # heartbeat.
            await asyncio.sleep(0)
            async for event in self._follow(owner, interaction_id, after_sequence):
                yield event
            return

        async for event in self._follow(owner, interaction_id, after_sequence):
            yield event

    async def cancel(
        self, credential: TrustedCredential, interaction_id: InteractionId
    ) -> InteractionRecord:
        """Persist a cancelled terminal state and stop the remaining call budget."""
        bind_interaction_id(str(interaction_id))
        owner, _ = await self._resolve_owner(credential)
        record = await self._load(owner, interaction_id)
        if record.status in TERMINAL_STATUSES:
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
        await self._commit(
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
        # In-process cancel channel: the durable terminal above is
        # the cross-process/restart channel; this event wakes the background
        # executor (if it runs in this process) at its next cooperative stop
        # point so it stops spending the remaining call budget.
        executor = self._executors.get(str(interaction_id))
        if executor is not None:
            executor.request_cancel()
        return cancelled
