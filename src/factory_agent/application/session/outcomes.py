"""Session outcome events: phases, clarifications, chat answers, terminations."""

from collections.abc import AsyncIterator
from dataclasses import replace

from factory_agent.application.intent import clarification_for
from factory_agent.application.permission_matrix import ROLE_DATA_RANGE
from factory_agent.application.session.base import SessionCore
from factory_agent.application.session.definitions import (
    PROGRESS_LABELS,
    STAGE_LABELS,
    STOP_RUN_TIMEOUT,
    RunState,
    session_logger,
)
from factory_agent.application.usage import drain_mes_events
from factory_agent.domain import (
    INTERACTION_ANSWER,
    INTERACTION_CLARIFICATION,
    INTERACTION_PHASE,
    INTERACTION_PROGRESS,
    CapabilityIntent,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    MessageRole,
    Role,
    SessionEvent,
    SessionState,
    terminal_event_name,
)
from factory_agent.ports import InteractionCommit, UsageEvent


def friendly_rejection(role: Role | None) -> str:
    """Friendly denial naming the caller's actual data range (权限不足友好提示)."""
    if role is None:
        return "当前角色暂不支持该查询。"
    data_range = ROLE_DATA_RANGE.get(role)
    if data_range is None:
        return "当前角色暂不支持该查询。"
    return f"当前角色暂不支持该查询。您可查询的范围：{data_range}。"


class SessionOutcomeMixin(SessionCore):
    """Terminal and clarification event emission, shared by the pipeline."""

    async def _clarify(
        self,
        state: RunState,
        intent: CapabilityIntent,
        usage_events: list[UsageEvent],
        *,
        rewrite_query: str | None = None,
    ) -> AsyncIterator[SessionEvent]:
        question = clarification_for(intent) or "请补充更多信息。"
        if rewrite_query and rewrite_query != state.record.input_text:
            # A multi-turn follow-up was rewritten into a standalone query;
            # echo it so the user can confirm the understood question.
            question = f"您想问的是：“{rewrite_query}”。{question}"
        async for event in self._clarify_message(state, question, usage_events):
            yield event

    async def _clarify_message(
        self,
        state: RunState,
        question: str,
        usage_events: list[UsageEvent],
    ) -> AsyncIterator[SessionEvent]:
        clarifying = self._advance(state.record, SessionState.CLARIFYING, "slots_missing")
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_CLARIFICATION,
            data={
                "question": question,
                "missing": [],
                "ambiguous": [],
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
        session_logger.info(
            "session.outcome.clarify question={question}",
            question=question,
            interaction_id=str(state.record.interaction_id),
            session_id=str(state.record.session_id),
        )
        usage_events.append(self._completion_event(state.record, result=None, error_category=None))
        await self._commit(
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
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield event
        yield terminal

    async def _chat_with_text(
        self,
        state: RunState,
        text: str,
        usage_events: list[UsageEvent],
    ) -> AsyncIterator[SessionEvent]:
        """Persist and yield one free-form chit-chat answer without a model call.

        Shared by the merged single-call path (text came from the intent call)
        and the ChatResponder fallback (text came from a dedicated CHAT call).
        """
        now = self._clock.now()
        answered = self._advance(state.record, SessionState.ANSWERED, "chat_answer")
        answer_event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_ANSWER,
            data={"text": text},
        )
        terminal = SessionEvent(
            sequence=state.next_sequence(),
            name=terminal_event_name(InteractionStatus.COMPLETED),
            data={
                "interaction_id": str(state.record.interaction_id),
                "status": "completed",
            },
        )
        state.record = replace(
            answered,
            status=InteractionStatus.COMPLETED,
            last_event_sequence=terminal.sequence,
            updated_at=now,
            completed_at=now,
        )
        session_logger.info(
            "session.outcome.chat answer={answer}",
            answer=text,
            interaction_id=str(state.record.interaction_id),
            session_id=str(state.record.session_id),
        )
        usage_events.append(self._completion_event(state.record, result=None, error_category=None))
        await self._commit(
            InteractionCommit(
                interaction=state.record,
                messages=(
                    self._message(
                        state.record,
                        MessageRole.ASSISTANT,
                        MessageKind.CHAT,
                        terminal.sequence,
                        text,
                    ),
                ),
                events=(answer_event, terminal),
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield answer_event
        yield terminal

    async def _phase(self, state: RunState, target: SessionState, reason: str) -> SessionEvent:
        """Announce and persist a completed stage transition."""
        advanced = self._advance(state.record, target, reason)
        return await self._stage_event(
            state,
            name=INTERACTION_PHASE,
            data={
                "state": target.value,
                "reason": reason,
                "stage": STAGE_LABELS.get(target, target.value),
                "status": "ok",
            },
            record=advanced,
        )

    async def _progress(self, state: RunState, reason: str) -> SessionEvent:
        """Announce that a long stage is under way, without moving the state.

        The slowest work runs while ``SessionState`` is still ``PARSING``:
        parsing, the authorization chain, and the directory resolution that
        chain depends on. Labelling it by advancing the state machine is not an
        option — ``PARSING -> PARSING`` is not a legal transition, and entering
        ``AUTHORIZING`` before the chain would turn a directory-ambiguity
        clarification into an illegal ``AUTHORIZING -> CLARIFYING`` one. So the
        display label travels in ``stage`` while ``state`` keeps reporting
        where the interaction really is.

        The commit is informational (``lifecycle=False``): every announced
        window sits before a cooperative stop point, so rewriting the durable
        status here would overwrite a terminal another process just persisted.
        """
        return await self._stage_event(
            state,
            name=INTERACTION_PROGRESS,
            data={
                "state": state.record.state.value,
                "reason": reason,
                "stage": PROGRESS_LABELS.get(reason, reason),
                "status": "running",
            },
            record=state.record,
            lifecycle=False,
        )

    async def _stage_event(
        self,
        state: RunState,
        *,
        name: str,
        data: dict[str, object],
        record: InteractionRecord,
        lifecycle: bool = True,
    ) -> SessionEvent:
        """Allocate the next sequence, persist the stage event, wake followers."""
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=name,
            data={**data, "duration_ms": state.duration_ms()},
        )
        state.record = replace(
            record, last_event_sequence=event.sequence, updated_at=self._clock.now()
        )
        await self._commit(
            InteractionCommit(
                interaction=state.record,
                events=(event,),
                usage_events=drain_mes_events(),
                lifecycle=lifecycle,
            )
        )
        return event

    async def _fail(
        self, state: RunState, category: str, usage_events: list[UsageEvent]
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._terminate(
            state, InteractionStatus.FAILED, category, usage_events, "查询未能完成。"
        ):
            yield event

    async def _reject(
        self,
        state: RunState,
        category: str,
        usage_events: list[UsageEvent],
        *,
        role: Role | None = None,
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._reject_message(
            state, category, friendly_rejection(role), usage_events
        ):
            yield event

    async def _reject_message(
        self,
        state: RunState,
        category: str,
        message: str,
        usage_events: list[UsageEvent],
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._terminate(
            state,
            InteractionStatus.FAILED,
            category,
            usage_events,
            message,
        ):
            yield event

    async def _terminate(
        self,
        state: RunState,
        status: InteractionStatus,
        category: str,
        usage_events: list[UsageEvent],
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
                "message": text,
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
        session_logger.info(
            "session.outcome.terminated state={state} status={status} category={category}",
            state=target.value,
            status=status.value,
            category=category,
            interaction_id=str(state.record.interaction_id),
            session_id=str(state.record.session_id),
        )
        await self._commit(
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
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield event

    async def _stop_here(
        self,
        state: RunState,
        usage_events: list[UsageEvent],
        stop: str,
    ) -> AsyncIterator[SessionEvent]:
        """Terminal handling at a cooperative stop point.

        ``run_timeout`` fails the run durably here; ``cancelled`` has its
        terminal already persisted by the cancel API, so the executor just
        exits without spending any further call budget.
        """
        if stop == STOP_RUN_TIMEOUT:
            async for event in self._fail(state, "run_timeout", usage_events):
                yield event
