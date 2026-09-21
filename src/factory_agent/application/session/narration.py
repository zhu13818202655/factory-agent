"""Interleaving one round's reasoning transcript with the work it describes.

Narration must never lengthen an answer. Every narration call therefore runs
concurrently with the step it describes and is abandoned the moment that step
finishes, which makes the transcript a *partial* account of each step — the
honest outcome, since the block exists to fill a wait rather than to be a
complete log.

Two sources feed the one transcript, and the split is what makes the feature
work on a deployment whose gateway cannot stream at all:

* the **model** narrates the two waits where nothing else is visible — the
  intent call, whose window opens before anything about the question is known,
  and the capability run, where the factory system does the slow work;
* the **pipeline** states facts it already holds — the stage just entered, and
  the pager's own page and row counters. Those counters are the only honest
  account of the MES wait: no model is reasoning during it, so narrating
  "model thoughts" there would be fabrication rather than reporting.

Fact sentences alone are a complete transcript, which is why narration still
works with ``stream_gateway=None``. The model only adds wording on top.

This module drives the transcript; emitting its frames lives one layer down, in
``outcomes``, because every terminal path there closes the block before it
allocates a terminal event.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Literal, cast

from factory_agent.application.session.consistency import SessionConsistencyMixin
from factory_agent.application.session.definitions import RunState, session_logger
from factory_agent.application.session.thinking import (
    FetchProgressWatch,
    ThinkingFacts,
    ThinkingTranscript,
    build_thinking_messages,
)
from factory_agent.application.usage import llm_call_event
from factory_agent.domain import SessionEvent
from factory_agent.ports import (
    ModelGatewayError,
    ModelRequest,
    ModelStage,
    UsageEvent,
    report_mes_fetch_progress,
)


@dataclass(slots=True)
class WorkSlot[T]:
    """Carries a concurrently-run step's outcome back to its caller.

    A generator cannot ``return`` a value through ``async for``, so the awaited
    step writes here and the driver re-raises whatever it captured.
    """

    value: T | None = None
    error: BaseException | None = None


def settled[T](slot: WorkSlot[T]) -> T:
    """The step's outcome, re-raising whatever it captured."""
    if slot.error is not None:
        raise slot.error
    return cast("T", slot.value)


class SessionNarrationMixin(SessionConsistencyMixin):
    """Drives ``interaction.thinking`` for one interaction round."""

    def _start_transcript(self, state: RunState, facts: ThinkingFacts) -> None:
        """Open the round's transcript.

        Called once per interaction. Everything the round narrates afterwards
        lands in this one block, because the contract numbers fragments per
        round and emits one ``kind=thinking`` message per round.
        """
        state.transcript = ThinkingTranscript(facts=facts, enabled=self._limits.thinking_enabled)

    async def _narrate_over[T](
        self,
        state: RunState,
        work: Awaitable[T],
        slot: WorkSlot[T],
        usage_events: list[UsageEvent],
        *,
        facts: ThinkingFacts,
        watch: FetchProgressWatch | None = None,
    ) -> AsyncIterator[SessionEvent]:
        """Run ``work`` while streaming its narration; leave the outcome in ``slot``.

        Frames are emitted from inside the wait, so the client sees the
        transcript accumulate while the step is still running. The caller reads
        the outcome with :func:`settled` after the loop, which is also what
        re-raises a step failure — the failure never shortens the transcript.
        """
        transcript = state.transcript
        if (
            transcript is None
            or not transcript.enabled
            or transcript.closed
            or self._stream_gateway is None
        ):
            # Nothing to interleave. A step is offloaded into a task only so its
            # model stream can be poured out while it runs; with no streaming
            # gateway there is no stream, and the offload would merely move the
            # step one scheduling hop later. That hop is not free: the follower
            # tails the durable event log, so a hop with no commit in it reads as
            # silence and draws a heartbeat. The deterministic frames the caller
            # emits around the step are what cover the wait instead.
            await self._settle(work, slot, None)
            return
        transcript.facts = transcript.facts.merged_with(facts)
        poll = watch.fresh_sentence if watch is not None else None
        look = asyncio.ensure_future(self._settle(work, slot, watch))
        narration = asyncio.ensure_future(
            self._consume_narration(state, transcript, facts, usage_events)
        )
        try:
            while not look.done():
                await asyncio.wait({look}, timeout=self._limits.thinking_tick_seconds)
                async for event in self._flush_frames(state, transcript, poll=poll):
                    yield event
            # The step is over: one last sweep for what already arrived, then the
            # narration is abandoned rather than awaited — finishing the
            # sentence must never be the reason an answer is late.
            async for event in self._flush_frames(state, transcript, poll=poll, force=True):
                yield event
        finally:
            await self._abandon(narration)
            if look.done():
                # Retrieve the outcome so a state the loop never consumed (a
                # cancellation racing the last tick) is not reported as an
                # unretrieved task exception at loop shutdown.
                with contextlib.suppress(BaseException):
                    look.result()
            else:
                look.cancel()
                with contextlib.suppress(BaseException):
                    await look

    async def _settle[T](
        self, work: Awaitable[T], slot: WorkSlot[T], watch: FetchProgressWatch | None
    ) -> None:
        """Await ``work`` into ``slot``, publishing its fetch pages while it waits.

        The sink is installed *inside* this task rather than around the caller:
        the pager sits three layers down and is shared across concurrent
        interactions, so the registration has to be scoped to the one task that
        is running this step. That also makes it impossible for one tenant's
        page count to surface in another tenant's transcript.
        """
        try:
            if watch is None:
                slot.value = await work
            else:
                with report_mes_fetch_progress(watch.observe):
                    slot.value = await work
        except Exception as exc:  # noqa: BLE001 - re-raised by the driver
            slot.error = exc

    async def _consume_narration(
        self,
        state: RunState,
        transcript: ThinkingTranscript,
        facts: ThinkingFacts,
        usage_events: list[UsageEvent],
    ) -> None:
        """Stream one narration call into the round's transcript.

        Failure is contained on purpose: the transcript is an aid, so a gateway
        refusal, a protocol error, or an unreadable stream must leave the
        business answer untouched. The call is still metered, because a failed
        narration is still spend.
        """
        gateway = self._stream_gateway
        if gateway is None:
            return
        request = ModelRequest(
            model_alias=self._thinking_model_alias,
            messages=build_thinking_messages(facts),
            stage=ModelStage.THINKING,
            logical_call_id=self._new_id(),
            temperature=0.3,
            max_output_tokens=self._limits.thinking_max_output_tokens,
            timeout_seconds=self._limits.thinking_timeout_seconds,
        )
        started = time.monotonic()
        status: Literal["completed", "failed"] = "completed"
        try:
            async for delta in gateway.stream(request):
                transcript.coalescer.feed(delta.text)
        except asyncio.CancelledError:
            raise
        except ModelGatewayError as exc:
            status = "failed"
            session_logger.warning(
                "session.thinking.stream_failed",
                category=exc.category.value,
                interaction_id=str(state.record.interaction_id),
            )
        except Exception:
            status = "failed"
            session_logger.opt(exception=True).warning(
                "session.thinking.stream_failed",
                interaction_id=str(state.record.interaction_id),
            )
        finally:
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=self._clock.now(),
                    logical_call_id=request.logical_call_id,
                    stage=ModelStage.THINKING,
                    model_alias=request.model_alias,
                    # The alias, not a deployment id: the router picks the
                    # deployment inside the SDK and a streamed call never
                    # reports back which one served it.
                    actual_model=request.model_alias,
                    attempt=1,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status=status,
                )
            )

    @staticmethod
    async def _abandon(task: asyncio.Task[None]) -> None:
        """Drop a narration task without letting it disturb the run."""
        if task.done():
            with contextlib.suppress(BaseException):
                task.result()
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
