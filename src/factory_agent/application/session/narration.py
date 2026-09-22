"""Interleaving one round's reasoning transcript with the work it describes.

Narration must never lengthen an answer. The interleaving loop therefore runs
concurrently with the step it describes and is abandoned the moment that step
finishes, which makes the transcript a *partial* account of each step — the
honest outcome, since the block exists to fill a wait rather than to be a
complete log.

Every frame is a **fixed reviewed sentence**, never model output:

* the **pipeline** states facts it already holds — the stage just entered, the
  capability's reviewed title, and the resolved window, emitted by ``pipeline``
  itself around the step;
* the **watch** publishes the live counters and the elapsed wait while the
  step is still running — the pager's page and row counters during the fetch,
  and a fixed "已等待 N 秒" sentence every ``wait_seconds`` in any quiet
  window. Those counters are the only honest account of the MES wait: no
  model is reasoning during it, so narrating "model thoughts" there would be
  fabrication rather than reporting.

Fixed sentences alone are a complete transcript on every deployment, and the
narration costs zero LLM spend.

This module drives the transcript; emitting its frames lives one layer down, in
``outcomes``, because every terminal path there closes the block before it
allocates a terminal event.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import cast

from factory_agent.application.session.consistency import SessionConsistencyMixin
from factory_agent.application.session.definitions import RunState
from factory_agent.application.session.thinking import (
    FetchProgressWatch,
    ThinkingFacts,
    ThinkingTranscript,
)
from factory_agent.domain import SessionEvent
from factory_agent.ports import report_mes_fetch_progress


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
        *,
        facts: ThinkingFacts,
        watch: FetchProgressWatch | None = None,
    ) -> AsyncIterator[SessionEvent]:
        """Run ``work`` while streaming its fixed narration; leave the outcome in ``slot``.

        Frames are emitted from inside the wait, so the client sees the
        transcript accumulate while the step is still running. The caller reads
        the outcome with :func:`settled` after the loop, which is also what
        re-raises a step failure — the failure never shortens the transcript.
        """
        transcript = state.transcript
        if transcript is None or not transcript.enabled or transcript.closed:
            # Narration is switched off. A step is offloaded into a task only so
            # its narration can be poured out while it runs; with nothing to
            # interleave the offload would merely move the step one scheduling
            # hop later. That hop is not free: the follower tails the durable
            # event log, so a hop with no commit in it reads as silence and
            # draws a heartbeat.
            await self._settle(work, slot, watch)
            return
        transcript.facts = transcript.facts.merged_with(facts)
        poll = watch.fresh_sentence if watch is not None else None
        look = asyncio.ensure_future(self._settle(work, slot, watch))
        try:
            while not look.done():
                await asyncio.wait({look}, timeout=self._limits.thinking_tick_seconds)
                async for event in self._flush_frames(state, transcript, poll=poll):
                    yield event
            # The step is over: one last sweep for a sentence that became due
            # on the very tick the step finished on.
            async for event in self._flush_frames(state, transcript, poll=poll):
                yield event
        finally:
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
