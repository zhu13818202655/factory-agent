"""Executor lifecycle: spawn, follow, stale-run self-heal, shutdown drain."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from factory_agent.application.authorization import ResolvedAuthorization
from factory_agent.application.context import ConversationTurn
from factory_agent.application.session.base import SessionCore
from factory_agent.application.session.definitions import (
    ABANDONED_NOTICE,
    CATEGORY_ABANDONED,
    CATEGORY_EXECUTOR_LOST,
    TERMINAL_NAMES,
    DrillPayload,
    session_logger,
)
from factory_agent.application.session.executor import InteractionRunExecutor
from factory_agent.domain import (
    INTERACTION_HEARTBEAT,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    MessageRole,
    SessionEvent,
    terminal_event_name,
)
from factory_agent.ports import InteractionCommit, InteractionOwner, TrustedCredential

if TYPE_CHECKING:
    from factory_agent.application.session.service import SessionService


class SessionLifecycleMixin(SessionCore):
    """Background executor registry and recovery sweeps."""

    async def _follow(
        self, owner: InteractionOwner, interaction_id: InteractionId, after_sequence: int
    ) -> AsyncIterator[SessionEvent]:
        """Tail an interaction the background executor is running; never re-run it."""
        waited = 0.0
        key = str(interaction_id)
        while waited <= self._limits.follow_timeout_seconds:
            events = await self._store.list_events(owner, interaction_id, after_sequence)
            for event in events:
                yield event
                after_sequence = event.sequence
                if event.name in TERMINAL_NAMES:
                    self._notifications.pop(key, None)
                    return
            # Self-heal: the background executor may have died without
            # persisting a terminal event; a stale ``running`` row is failed
            # durably so this and every future connection terminates cleanly.
            healed = await self._fail_stale_run(owner, interaction_id)
            if healed is not None:
                yield healed
                return
            if not events:
                # Wait for the in-process executor's progress signal; fall back
                # to a heartbeat after one quiet interval.
                progressed = await self._wait_for_progress(key)
                if not progressed:
                    yield SessionEvent(
                        sequence=after_sequence,
                        name=INTERACTION_HEARTBEAT,
                        data={"interaction_id": str(interaction_id)},
                    )
                    waited += self._limits.heartbeat_seconds
        # Follow budget exhausted with the run still fresh: end this stream with
        # an explicit wire-only terminal (sequence+1 passes the client's event
        # dedup) so the client stops waiting. The interaction row is untouched;
        # a genuinely slow executor can still persist its outcome.
        self._notifications.pop(key, None)
        yield SessionEvent(
            sequence=after_sequence + 1,
            name=terminal_event_name(InteractionStatus.FAILED),
            data={"interaction_id": str(interaction_id), "error_category": "follow_timeout"},
        )

    async def _fail_stale_run(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> SessionEvent | None:
        """Durably fail an orphaned run and return its terminal event.

        A run is orphaned when its executor died before persisting a terminal
        event (process exit, crash outside the pipeline's guard); the row stays
        ``running`` and no reconnect can ever observe a terminal. The
        compare-and-set keeps this race-safe against a live executor commit.
        """
        now = self._clock.now()
        record = await self._store.fail_stale_run(
            owner,
            interaction_id,
            stale_before=now - timedelta(seconds=self._limits.stale_running_seconds),
            now=now,
            category=CATEGORY_EXECUTOR_LOST,
        )
        if record is None:
            return None
        return await self._persist_reaped_terminal(
            record,
            category=CATEGORY_EXECUTOR_LOST,
            text="查询未能完成。",
            # A run that died after its ``started`` event already advanced
            # ``last_event_sequence`` past the question's own message, so the
            # reserved sequence is free for the error message too.
            message_sequence=record.last_event_sequence,
        )

    async def _persist_reaped_terminal(
        self,
        record: InteractionRecord,
        *,
        category: str,
        text: str,
        message_sequence: int,
    ) -> SessionEvent:
        """Commit the terminal event of an interaction a sweep already failed.

        The sweep's compare-and-set reserved the terminal *event* sequence in
        ``last_event_sequence``; ``message_sequence`` is passed separately
        because ``agent_message`` carries its own unique
        ``(interaction_id, sequence)`` key and a never-claimed interaction
        already holds sequence 1 for the user's question.
        """
        event = SessionEvent(
            sequence=record.last_event_sequence,
            name=terminal_event_name(InteractionStatus.FAILED),
            data={
                "interaction_id": str(record.interaction_id),
                "error_category": category,
                "message": text,
            },
        )
        await self._commit(
            InteractionCommit(
                interaction=record,
                messages=(
                    self._message(
                        record,
                        MessageRole.SYSTEM,
                        MessageKind.ERROR,
                        message_sequence,
                        text,
                    ),
                ),
                events=(event,),
                usage_events=(
                    self._completion_event(record, result=None, error_category=category),
                ),
            )
        )
        return event

    async def sweep_stale_runs(self) -> int:
        """Durably fail every orphaned ``running`` interaction.

        A previous process may have died with interactions still ``running``,
        and a run whose followers all disconnected is never observed by
        ``_follow``. The bulk compare-and-set is idempotent and safe under
        multi-worker starts and repeated periodic passes — only rows still
        ``running`` and stale are marked ``executor_lost``, each exactly once.
        """
        now = self._clock.now()
        records = await self._store.fail_stale_runs(
            stale_before=now - timedelta(seconds=self._limits.stale_running_seconds),
            now=now,
            category=CATEGORY_EXECUTOR_LOST,
        )
        for record in records:
            await self._persist_reaped_terminal(
                record,
                category=CATEGORY_EXECUTOR_LOST,
                text="查询未能完成。",
                message_sequence=record.last_event_sequence,
            )
        if records:
            session_logger.warning(
                "session.sweep.executor_lost count={count}",
                count=len(records),
            )
        return len(records)

    async def sweep_abandoned_runs(self) -> int:
        """Durably fail every ``pending`` interaction that never ran.

        ``start`` persists a question as ``pending`` and only the first
        claiming stream runs it, so a question whose client never subscribed
        would otherwise stay ``pending`` forever: counted by metering, with no
        answer and no terminal event. Rows older than
        ``abandoned_pending_seconds`` are terminated here. Idempotent and safe
        under concurrent workers: the compare-and-set only touches rows still
        ``pending``, each exactly once.
        """
        now = self._clock.now()
        records = await self._store.fail_abandoned_runs(
            abandoned_before=now - timedelta(seconds=self._limits.abandoned_pending_seconds),
            now=now,
            category=CATEGORY_ABANDONED,
        )
        for record in records:
            await self._persist_reaped_terminal(
                record,
                category=CATEGORY_ABANDONED,
                text=ABANDONED_NOTICE,
                # A never-claimed interaction produced no event at all, so its
                # only message is the question at sequence 1; the terminal event
                # takes the reserved sequence and the error message the next one.
                message_sequence=record.last_event_sequence + 1,
            )
        if records:
            session_logger.warning("session.sweep.abandoned count={count}", count=len(records))
        return len(records)

    async def sweep_forever(self, interval_seconds: float) -> None:
        """Periodic recovery: reap abandoned questions and orphaned runs.

        Runs until the application lifespan cancels it. Both sweeps are
        compare-and-set based, so running this loop in every worker is safe and
        a failing pass never stops the loop — recovery must not become a second
        outage. Pacing uses the real clock on purpose: this cadence is
        wall-clock, unlike the injected sleep that paces the follow loops.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.sweep_abandoned_runs()
                await self.sweep_stale_runs()
            except Exception:  # noqa: BLE001 - recovery must never kill the loop
                session_logger.exception("session.sweep.periodic_failed")

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Bounded drain of the in-process run executors at shutdown.

        Executors finishing within the budget keep persisting their outcome;
        the rest are cancelled and recovered as orphans by the next startup
        sweep (bounded drain, no indefinite wait — container
        rebuilds must not hang on a stuck pipeline).
        """
        tasks = [
            executor.task
            for executor in self._executors.values()
            if executor.task is not None and not executor.task.done()
        ]
        if not tasks:
            return
        session_logger.info("session.shutdown.draining count={count}", count=len(tasks))
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _spawn_executor(
        self,
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        claimed: InteractionRecord,
        history: tuple[ConversationTurn, ...],
        credential: TrustedCredential,
        *,
        drill: "DrillPayload | None" = None,
    ) -> InteractionRunExecutor:
        """Hand a claimed run to a background executor."""
        executor = InteractionRunExecutor(
            cast("SessionService", self),
            owner,
            authorization,
            claimed,
            history,
            credential,
            drill=drill,
        )
        self._executors[str(claimed.interaction_id)] = executor
        executor.start()
        return executor

    def _unregister_executor(self, executor: InteractionRunExecutor) -> None:
        key = str(executor.interaction_id)
        self._executors.pop(key, None)
        self._notifications.pop(key, None)
