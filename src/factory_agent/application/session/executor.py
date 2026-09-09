"""Background executor owning one claimed interaction pipeline."""

import asyncio
import time
from typing import TYPE_CHECKING

from factory_agent.application.authorization import ResolvedAuthorization
from factory_agent.application.context import ConversationTurn
from factory_agent.application.session.definitions import (
    STOP_CANCELLED,
    STOP_RUN_TIMEOUT,
    TERMINAL_STATUSES,
    session_logger,
)
from factory_agent.domain import InteractionId, InteractionRecord
from factory_agent.ports import InteractionOwner, TrustedCredential

if TYPE_CHECKING:
    from factory_agent.application.session.service import SessionService


class InteractionRunExecutor:
    """Background owner of one claimed interaction pipeline.

    The executor is spawned by the claiming SSE connection but its lifecycle
    is bound to the interaction, never to that connection: it holds the
    credential, usage context, and credential binding inside its own task
    context, drives the pipeline to a durable terminal state event by event,
    and dies only when the run ends. Connections may disconnect, reconnect,
    or tail concurrently without affecting it.

    Cancellation is dual-channel: the cancel API persists the ``cancelled``
    terminal (cross-process and restart-safe) and sets the in-process event
    (``request_cancel``) that this task observes at its cooperative stop
    points. A whole-run wall-clock budget (``run_timeout_seconds``) is
    checked at the same stop points and fails the run durably with
    ``run_timeout`` — the budget catches a run that is alive but dragging,
    while ``fail_stale_run`` self-heal catches one that died silently.
    """

    def __init__(
        self,
        service: "SessionService",
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        record: InteractionRecord,
        history: tuple[ConversationTurn, ...],
        credential: TrustedCredential,
    ) -> None:
        self._service = service
        self._owner = owner
        self._authorization = authorization
        self._record = record
        self._history = history
        self._credential = credential
        self._cancel_event = asyncio.Event()
        # Deliberate friend-class access: the executor and the service share
        # one module on purpose (same lifecycle, same invariants).
        self._deadline = time.monotonic() + service._limits.run_timeout_seconds  # pyright: ignore[reportPrivateUsage]
        self.task: asyncio.Task[None] | None = None

    @property
    def interaction_id(self) -> InteractionId:
        return self._record.interaction_id

    def start(self) -> None:
        self.task = asyncio.create_task(
            self._execute(), name=f"interaction-run-{self._record.interaction_id}"
        )

    def request_cancel(self) -> None:
        """In-process cancel signal, observed at the next cooperative stop point."""
        self._cancel_event.set()

    async def interrupted(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> str | None:
        """Stop-point verdict: ``None`` keeps running.

        Checks the in-process cancel event, the wall-clock budget, and — the
        cross-process/restart channel — the durable interaction status.
        """
        if self._cancel_event.is_set():
            return STOP_CANCELLED
        if time.monotonic() >= self._deadline:
            return STOP_RUN_TIMEOUT
        record = await self._service._store.get_interaction(owner, interaction_id)  # pyright: ignore[reportPrivateUsage]
        if record is not None and record.status in TERMINAL_STATUSES:
            # Another process (or the cancel API) already wrote a terminal;
            # this task must not resurrect it or spend further call budget.
            return STOP_CANCELLED
        return None

    async def _execute(self) -> None:
        try:
            # Events are persisted at every commit before they are yielded;
            # connections replay them from the store, so the task discards the
            # in-memory stream and only the durable outcome matters.
            async for _event in self._service._run(  # pyright: ignore[reportPrivateUsage]
                self._owner,
                self._authorization,
                self._record,
                self._history,
                0,
                self._credential,
                control=self,
            ):
                pass
        except asyncio.CancelledError:
            # Shutdown drain timeout only: a connection can never cancel this
            # task. The interaction stays ``running``; the next startup sweep
            # (or a follower's stale check) marks it ``executor_lost``.
            raise
        except Exception:
            # _run's internal guard already tried to persist a terminal; this
            # is the best-effort net for a crash before that guard existed.
            session_logger.exception(
                "session.executor.crashed",
                interaction_id=str(self._record.interaction_id),
            )
            try:
                await self._service._fail_stale_run(  # pyright: ignore[reportPrivateUsage]
                    self._owner, self._record.interaction_id
                )
            except Exception:  # noqa: BLE001 - recovery must never mask the crash
                session_logger.exception(
                    "session.executor.recovery_failed",
                    interaction_id=str(self._record.interaction_id),
                )
        finally:
            self._service._unregister_executor(self)  # pyright: ignore[reportPrivateUsage]
