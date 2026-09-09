"""Executor lifecycle: spawn, follow, stale-run self-heal, shutdown drain."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from factory_agent.application.authorization import ResolvedAuthorization
from factory_agent.application.context import ConversationTurn
from factory_agent.application.session.base import SessionCore
from factory_agent.application.session.definitions import TERMINAL_NAMES, session_logger
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
    """Background executor registry and orphan-run recovery."""

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
            category="executor_lost",
        )
        if record is None:
            return None
        return await self._persist_executor_lost(record)

    async def _persist_executor_lost(self, record: InteractionRecord) -> SessionEvent:
        """Commit the terminal event of an already CAS-failed orphaned run."""
        event = SessionEvent(
            sequence=record.last_event_sequence,
            name=terminal_event_name(InteractionStatus.FAILED),
            data={"interaction_id": str(record.interaction_id), "error_category": "executor_lost"},
        )
        await self._commit(
            InteractionCommit(
                interaction=record,
                messages=(
                    self._message(
                        record,
                        MessageRole.SYSTEM,
                        MessageKind.ERROR,
                        event.sequence,
                        "查询未能完成。",
                    ),
                ),
                events=(event,),
                usage_events=(
                    self._completion_event(record, result=None, error_category="executor_lost"),
                ),
            )
        )
        return event

    async def sweep_stale_runs(self) -> int:
        """Startup recovery: durably fail every orphaned ``running`` interaction.

        Runs once when the application starts: a previous process
        may have died with interactions still ``running``. The bulk
        compare-and-set is idempotent and safe under multi-worker starts —
        only rows still ``running`` and stale are marked ``executor_lost``,
        and each is marked exactly once.
        """
        now = self._clock.now()
        records = await self._store.fail_stale_runs(
            stale_before=now - timedelta(seconds=self._limits.stale_running_seconds),
            now=now,
            category="executor_lost",
        )
        for record in records:
            await self._persist_executor_lost(record)
        if records:
            session_logger.warning(
                "session.sweep.executor_lost count={count}",
                count=len(records),
            )
        return len(records)

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
    ) -> InteractionRunExecutor:
        """Hand a claimed run to a background executor."""
        executor = InteractionRunExecutor(
            cast("SessionService", self), owner, authorization, claimed, history, credential
        )
        self._executors[str(claimed.interaction_id)] = executor
        executor.start()
        return executor

    def _unregister_executor(self, executor: InteractionRunExecutor) -> None:
        key = str(executor.interaction_id)
        self._executors.pop(key, None)
        self._notifications.pop(key, None)
