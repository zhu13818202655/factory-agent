"""Session outcome events: phases, clarifications, chat answers, terminations."""

from collections.abc import AsyncIterator, Callable
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
from factory_agent.application.session.thinking import ThinkingTranscript, transcript_line
from factory_agent.application.usage import drain_mes_events
from factory_agent.domain import (
    INTERACTION_ANSWER,
    INTERACTION_CLARIFICATION,
    INTERACTION_PHASE,
    INTERACTION_PROGRESS,
    INTERACTION_THINKING,
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
        async for event in self._close_transcript(state):
            yield event
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
        usage_events.append(
            self._completion_event(
                state.record,
                result=None,
                error_category=None,
                usage_events=tuple(usage_events),
            )
        )
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
        async for event in self._close_transcript(state):
            yield event
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
        usage_events.append(
            self._completion_event(
                state.record,
                result=None,
                error_category=None,
                usage_events=tuple(usage_events),
            )
        )
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
        """Announce and persist one completed stage transition."""
        (event,) = await self._phases(state, (target, reason))
        return event

    async def _phases(
        self, state: RunState, *transitions: tuple[SessionState, str]
    ) -> tuple[SessionEvent, ...]:
        """Announce adjacent stage transitions in a single commit.

        Stages with no work between them (``AUTHORIZING`` -> ``EXECUTING``) are
        persisted together so no follower can observe a half-advanced run. Each
        transition still emits its own event, in the same order and with the
        same sequence numbers, so the live, replay, and history readings remain
        identical.
        """
        events: list[SessionEvent] = []
        for target, reason in transitions:
            advanced = self._advance(state.record, target, reason)
            events.append(
                self._stage_event_uncommitted(
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
            )
        await self._commit(
            InteractionCommit(
                interaction=state.record,
                events=tuple(events),
                usage_events=drain_mes_events(),
            )
        )
        return tuple(events)

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

    async def _thinking(
        self,
        state: RunState,
        text: str,
        *,
        sequence: int,
        done: bool = False,
        truncated: bool = False,
    ) -> SessionEvent:
        """Emit one fragment of the round's reasoning transcript.

        ``sequence`` is the transcript's own fragment counter (契约 §3.2 ``seq``,
        restarting at 1 every round); it is deliberately independent of the
        durable event id that carries the frame, because the event id is also
        the replay cursor and must stay globally monotonic.

        Informational commit (``lifecycle=False``) for the same reason as
        ``_progress``: a narration fragment must never be the write that
        resurrects a run another process already terminated.
        """
        data: dict[str, object] = {
            "text": text,
            "seq": sequence,
            "done": done,
            "elapsed_ms": state.duration_ms(),
        }
        if truncated:
            data["truncated"] = True
        return await self._stage_event(
            state,
            name=INTERACTION_THINKING,
            data=data,
            record=state.record,
            lifecycle=False,
        )

    async def _record_thinking_message(
        self, state: RunState, *, text: str, elapsed_ms: int, sequence: int
    ) -> None:
        """Persist the reassembled transcript once, for history restore.

        The per-fragment events are the replay path (契约 §3); this single
        ``kind=thinking`` row is what a refreshed client renders collapsed
        (契约 §4). Written only when the model actually narrated something, so
        a round that produced no transcript leaves no empty block behind.

        ``sequence`` reuses the final fragment's event sequence — the same
        mapping ``result_table`` uses for its own result event — so no event
        number is consumed by a row that has no event.
        """
        if not text.strip():
            return
        message = self._message(
            state.record,
            MessageRole.ASSISTANT,
            MessageKind.THINKING,
            sequence,
            text,
            payload={"elapsed_ms": elapsed_ms},
        )
        await self._commit(
            InteractionCommit(
                interaction=state.record,
                messages=(message,),
                usage_events=drain_mes_events(),
                lifecycle=False,
            )
        )

    async def _fact(self, state: RunState, sentence: str) -> AsyncIterator[SessionEvent]:
        """Emit one deterministic sentence as part of the transcript.

        Deterministic rows are what make the block honest on a deployment whose
        gateway cannot stream: the pipeline states what it is doing from facts
        it already holds instead of leaving the user with a silent wait.
        """
        transcript = state.transcript
        if transcript is None or not transcript.enabled or transcript.closed:
            return
        for frame in self._commit_fact(transcript, sentence):
            transcript.seq += 1
            event = await self._thinking(state, frame, sequence=transcript.seq)
            transcript.last_event_sequence = event.sequence
            yield event

    async def _flush_frames(
        self,
        state: RunState,
        transcript: ThinkingTranscript,
        *,
        poll: Callable[[], str | None] | None = None,
        force: bool = False,
    ) -> AsyncIterator[SessionEvent]:
        """Emit every frame that is ready, in transcript order.

        Facts first: a page that has just landed is the newest thing the user
        can be told, and the model's own pending clause is older than it.
        ``poll`` is the pager's own counter reader, passed as a callable so this
        layer stays independent of whoever is doing the fetching.
        """
        frames: list[str] = []
        if poll is not None:
            sentence = poll()
            if sentence is not None:
                frames.extend(self._commit_fact(transcript, sentence))
        frames.extend(transcript.coalescer.take(transcript.facts, force=force))
        for frame in frames:
            transcript.seq += 1
            event = await self._thinking(state, frame, sequence=transcript.seq)
            transcript.last_event_sequence = event.sequence
            yield event

    async def _close_transcript(self, state: RunState) -> AsyncIterator[SessionEvent]:
        """Finish the transcript: last frames, the end marker, the saved copy.

        Every terminal path calls this before it allocates the terminal event's
        sequence, which is what keeps thinking frames earlier than ``result``
        (契约 §7-5). A round that narrated nothing produces no marker and no
        message, so a client never renders an empty block.
        """
        transcript = state.transcript
        if transcript is None or transcript.closed:
            return
        # Marked first: re-entering here after a partial close would emit a
        # second ``done`` frame, which the contract forbids outright.
        transcript.closed = True
        if not transcript.enabled:
            return
        async for event in self._flush_frames(state, transcript, force=True):
            yield event
        if not transcript.produced:
            return
        transcript.seq += 1
        # 契约 §3.2: the final fragment may carry no text.
        done = await self._thinking(
            state,
            "",
            sequence=transcript.seq,
            done=True,
            truncated=transcript.coalescer.truncated,
        )
        transcript.last_event_sequence = done.sequence
        await self._record_thinking_message(
            state,
            text=transcript.text,
            elapsed_ms=state.duration_ms(),
            sequence=done.sequence,
        )
        yield done

    @staticmethod
    def _commit_fact(transcript: ThinkingTranscript, sentence: str) -> tuple[str, ...]:
        """Widen the gate's allowlist with one fact sentence, then send it.

        The allowlist holds the bare sentence while the frame is the sentence
        on its own transcript line: the gate compares numbers, which the line
        shape does not change, whereas the prompt renders the allowlist back to
        the model and would show a stray leading newline.
        """
        transcript.facts = transcript.facts.widened(sentence)
        return transcript.coalescer.commit(transcript.facts, transcript_line(sentence))

    def _stage_event_uncommitted(
        self,
        state: RunState,
        *,
        name: str,
        data: dict[str, object],
        record: InteractionRecord,
    ) -> SessionEvent:
        """Allocate the next sequence and advance the in-memory run record."""
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=name,
            data={**data, "duration_ms": state.duration_ms()},
        )
        state.record = replace(
            record, last_event_sequence=event.sequence, updated_at=self._clock.now()
        )
        return event

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
        event = self._stage_event_uncommitted(state, name=name, data=data, record=record)
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
        async for event in self._close_transcript(state):
            yield event
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
            self._completion_event(
                state.record,
                result=None,
                error_category=category,
                usage_events=tuple(usage_events),
            )
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
            return
        # ``cancelled``: the cancel API already persisted the terminal, so the
        # executor spends no further call budget on one. The transcript is still
        # finished, so a cancelled round keeps — and can still restore — the
        # reasoning it had already shown (契约 §8-10).
        async for event in self._close_transcript(state):
            yield event
