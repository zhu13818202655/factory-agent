"""Session service core: dependency wiring and cross-cutting primitives."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from zoneinfo import ZoneInfo

from factory_agent.application.authorization import AuthorizationService
from factory_agent.application.business_filters import BusinessFilterResolver
from factory_agent.application.chitchat import ChatResponder
from factory_agent.application.consistency import ConsistencyValidator
from factory_agent.application.filters import FilterNarrower
from factory_agent.application.intent import CapabilityIntentParser
from factory_agent.application.personal import PersonalizationService
from factory_agent.application.scope_guard import ScopeGuard
from factory_agent.application.session.definitions import (
    DrillPayload,
    IdFactory,
    SessionLimits,
)
from factory_agent.application.session.executor import InteractionRunExecutor
from factory_agent.application.summary import ResultSummarizer
from factory_agent.application.time_expressions import DEFAULT_TIME_RANGE_MAX_DAYS
from factory_agent.application.usage import (
    LLM_CALL_EVENT_TYPE,
    UsageContext,
    completion_status,
    interaction_completed_event,
    new_trace_id,
)
from factory_agent.domain import (
    InteractionId,
    InteractionRecord,
    MessageId,
    MessageKind,
    MessageRecord,
    MessageRole,
    SessionState,
    SessionStateMachine,
)
from factory_agent.observability.audit import AuditSink
from factory_agent.ports import (
    CapabilityRunner,
    CapabilityRunResult,
    Clock,
    InteractionCommit,
    InteractionOwner,
    InteractionStore,
    UsageEvent,
)
from factory_agent.ports.artifacts import ArtifactExporter
from factory_agent.ports.contracts import CredentialBinder
from factory_agent.ports.scope_violation import ScopeViolationStore


def _llm_duration_ms(event: UsageEvent) -> int:
    """Wall-clock milliseconds one model call spent, failed attempts included."""
    if event.event_type != LLM_CALL_EVENT_TYPE:
        return 0
    value = event.payload.get("duration_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


class SessionCore:
    """Dependencies and primitives shared by every session mixin layer."""

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
        business_filters: BusinessFilterResolver | None = None,
        limits: SessionLimits | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        exporter: ArtifactExporter | None = None,
        personalization: PersonalizationService | None = None,
        credential_binder: CredentialBinder | None = None,
        time_range_max_days: int = DEFAULT_TIME_RANGE_MAX_DAYS,
        factory_timezone: str = "Asia/Shanghai",
        validator: ConsistencyValidator | None = None,
        violations: ScopeViolationStore | None = None,
        audit: AuditSink | None = None,
        chat: ChatResponder | None = None,
        summarizer: ResultSummarizer | None = None,
        scope_guard: ScopeGuard | None = None,
        validation_mode: str = "strict",
    ) -> None:
        self._store = store
        self._authorization = authorization
        self._parser = parser
        self._runner = runner
        self._clock = clock
        self._new_id = new_id
        self._narrower = narrower or FilterNarrower()
        self._business_filters = business_filters
        self._limits = limits or SessionLimits()
        self._sleep = sleep or asyncio.sleep
        self._exporter = exporter
        self._personalization = personalization
        self._chat_responder = chat
        self._summarizer = summarizer
        #: data scope against the token role range before any business call.
        self._scope_guard = scope_guard
        self._credential_binder = credential_binder
        self._time_range_max_days = time_range_max_days
        #: Factory-local zone for time-window labels; canonical windows are
        #: stored in UTC and must not be shown with UTC ``.date()`` values.
        self._factory_timezone_name = factory_timezone
        self._factory_zone = ZoneInfo(factory_timezone)
        #: In-process structured drill requests (D-3)，keyed by interaction id:
        #: stored by ``start`` and consumed once by the claiming stream. See
        #: ``DrillPayload`` for the restart semantics.
        self._drill_requests: dict[str, DrillPayload] = {}
        #: Role-consistency safety net: runs post-fetch, pre-compose.
        self._validator = validator
        self._violations = violations
        self._audit = audit
        self._validation_mode = validation_mode
        # Executor decoupling: claimed runs execute in background
        # tasks owned by the service, never by an SSE connection. The registry
        # keeps strong references (GC + cancel + shutdown drain); the
        # per-interaction events are the in-process progress signals that wake
        # following connections ahead of the heartbeat interval.
        self._executors: dict[str, InteractionRunExecutor] = {}
        self._notifications: dict[str, asyncio.Event] = {}

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
        usage_events: Sequence[UsageEvent] = (),
    ) -> UsageEvent:
        duration_ms = max(0, int((record.updated_at - record.created_at).total_seconds() * 1000))
        llm_duration_ms = sum(_llm_duration_ms(event) for event in usage_events)
        mes_duration_ms = result.duration_ms if result is not None else 0
        return interaction_completed_event(
            self._usage_context(record),
            occurred_at=record.updated_at,
            status=completion_status(record.status),
            duration_ms=duration_ms,
            mes_duration_ms=mes_duration_ms,
            llm_duration_ms=llm_duration_ms,
            # Residual: wall clock the interaction spent outside MES and model
            # calls. It includes queueing and waiting, so it is not a pure
            # "local computation" figure.
            local_duration_ms=max(0, duration_ms - llm_duration_ms - mes_duration_ms),
            result_row_count=len(result.rows) if result is not None else 0,
            error_category=error_category,
        )

    async def _commit(self, commit: InteractionCommit) -> None:
        """Persist a run commit, then signal tailing connections."""
        await self._store.commit(commit)
        self._notify(str(commit.interaction.interaction_id))

    def _notify(self, interaction_id: str) -> None:
        """Wake connections tailing this interaction after a persisted commit."""
        event = self._notifications.get(interaction_id)
        if event is not None:
            event.set()

    async def _wait_for_progress(self, key: str) -> bool:
        """Wait up to one heartbeat interval for the executor's progress signal.

        Returns ``True`` when the signal fired (a commit landed — poll again
        immediately), ``False`` on a quiet timeout (emit a heartbeat). Uses the
        injected sleep so offline tests keep their deterministic pacing.
        """
        event = self._notifications.get(key)
        if event is None:
            event = asyncio.Event()
            self._notifications[key] = event
        if event.is_set():
            event.clear()
            return True
        waiter = asyncio.ensure_future(event.wait())
        sleeper = asyncio.ensure_future(self._sleep(self._limits.heartbeat_seconds))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, sleeper):
                task.cancel()
            await asyncio.gather(waiter, sleeper, return_exceptions=True)
        progressed = waiter.done() and not waiter.cancelled()
        if progressed and self._notifications.get(key) is event:
            event.clear()
        return progressed

    async def _stop_reason(
        self,
        owner: InteractionOwner,
        interaction_id: InteractionId,
        control: InteractionRunExecutor | None,
    ) -> str | None:
        """Cooperative stop-point verdict: ``None`` keeps running."""
        if control is None:
            return None
        return await control.interrupted(owner, interaction_id)
