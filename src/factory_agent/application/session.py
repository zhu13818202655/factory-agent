"""Bounded session orchestration.

Replaces the report-agent monolithic ``ReportService``: this module owns the
state machine, clarification budget, idempotent execution, and durable event
sequence, while identity resolution, capability execution, and model calls stay
behind ports. Authorization always completes before any business-data call, and
scope identifiers reach the executor only through ``NarrowedFilters``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import timedelta

from factory_agent.application.authorization import (
    AuthorizationService,
    ResolvedAuthorization,
)
from factory_agent.application.business_filters import (
    BusinessFilterResolver,
    DirectoryError,
    ResolvedBusinessFilters,
)
from factory_agent.application.capability_map import fr_id_for
from factory_agent.application.consistency import (
    ConsistencyValidator,
    ConsistencyVerdict,
    ValidationAction,
    ValidationLevel,
)
from factory_agent.application.context import ConversationTurn
from factory_agent.application.filters import FilterNarrower, FilterRejectionError
from factory_agent.application.intent import CapabilityIntentParser, clarification_for
from factory_agent.application.permission_matrix import (
    ROLE_DATA_RANGE,
    Capability,
    authorize_capability,
)
from factory_agent.application.personal import PersonalizationService
from factory_agent.application.structured import StructuredOutputError
from factory_agent.application.usage import (
    UsageContext,
    completion_status,
    drain_mes_events,
    interaction_completed_event,
    interaction_started_event,
    llm_call_event,
    new_trace_id,
    set_usage_context,
)
from factory_agent.domain import (
    INTERACTION_CLARIFICATION,
    INTERACTION_HEARTBEAT,
    INTERACTION_PHASE,
    INTERACTION_RESULT,
    INTERACTION_STARTED,
    CapabilityId,
    CapabilityIntent,
    DataScope,
    ExpectedRange,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageId,
    MessageKind,
    MessageRecord,
    MessageRole,
    Role,
    SessionEvent,
    SessionId,
    SessionState,
    SessionStateMachine,
    TenantContext,
    TimeRange,
    terminal_event_name,
)
from factory_agent.domain.errors import ForbiddenError
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditSink,
)
from factory_agent.observability.logging_adapter import get_logger
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
    UsageEvent,
)
from factory_agent.ports.artifacts import ArtifactExporter
from factory_agent.ports.contracts import CredentialBinder
from factory_agent.ports.scope_violation import ScopeViolationRecord, ScopeViolationStore
from factory_agent.ports.session import CapabilityRunner

IdFactory = Callable[[], str]

_logger = get_logger("session.consistency")

#: Customer-confirmed time-range ceiling: at most the past year. Requests
#: beyond it are terminated with a friendly notice before any MES call.
DEFAULT_TIME_RANGE_MAX_DAYS = 366


class InteractionNotFoundError(LookupError):
    """Raised for both a missing interaction and one owned by another user."""


@dataclass(frozen=True, slots=True)
class SessionLimits:
    max_input_chars: int = 2000
    max_clarification_rounds: int = 3
    heartbeat_seconds: float = 15.0
    follow_timeout_seconds: float = 300.0


_EMPTY_BUSINESS_FILTERS = ResolvedBusinessFilters(
    employee_ids=None,
    dept_ids=None,
    order_codes=None,
    style_codes=None,
    plan_codes=None,
)


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
        business_filters: BusinessFilterResolver | None = None,
        limits: SessionLimits | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        exporter: ArtifactExporter | None = None,
        personalization: PersonalizationService | None = None,
        credential_binder: CredentialBinder | None = None,
        time_range_max_days: int = DEFAULT_TIME_RANGE_MAX_DAYS,
        validator: ConsistencyValidator | None = None,
        violations: ScopeViolationStore | None = None,
        audit: AuditSink | None = None,
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
        self._credential_binder = credential_binder
        self._time_range_max_days = time_range_max_days
        #: Role-consistency safety net (Story 2): runs post-fetch, pre-compose.
        self._validator = validator
        self._violations = violations
        self._audit = audit
        self._validation_mode = validation_mode

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
        interaction_id = InteractionId(self._new_id())
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
            async for event in self._run(
                owner, authorization, claimed, history, after_sequence, credential
            ):
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
        credential: TrustedCredential,
    ) -> AsyncIterator[SessionEvent]:
        state = _RunState(
            record=record,
            sequence=max(record.last_event_sequence, after_sequence),
            started_monotonic=time.monotonic(),
        )
        # The adapter meters every MES call at its ``_send`` exit; the active
        # usage context is bound for the duration of this run so business-data
        # calls carry the interaction identifiers. The credential binder scopes
        # every MES call in this run to the caller's own token bundle.
        set_usage_context(self._usage_context(record))
        binder = self._credential_binder
        binding = binder.bind_for(credential) if binder is not None else nullcontext()
        try:
            with binding:
                started = SessionEvent(
                    sequence=state.next_sequence(),
                    name=INTERACTION_STARTED,
                    data={
                        "interaction_id": str(record.interaction_id),
                        "session_id": str(record.session_id),
                        "state": SessionState.PARSING.value,
                        "stage": "接收",
                        "status": "accepted",
                    },
                )
                state.record = replace(
                    state.record,
                    status=InteractionStatus.RUNNING,
                    last_event_sequence=started.sequence,
                    updated_at=self._clock.now(),
                )
                await self._store.commit(
                    InteractionCommit(
                        interaction=state.record,
                        events=(started,),
                        usage_events=drain_mes_events(),
                    )
                )
                yield started

                async for event in self._pipeline(owner, authorization, state, history):
                    yield event
                if state.record.status in _TERMINAL_STATUSES:
                    await self._record_history(owner, state)
        finally:
            set_usage_context(None)

    async def _pipeline(
        self,
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        state: _RunState,
        history: tuple[ConversationTurn, ...],
    ) -> AsyncIterator[SessionEvent]:
        usage_events: list[UsageEvent] = []
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

        state.last_intent = intent
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

        # Re-resolve scope after parsing so a context patch can never reuse an
        # older, broader scope. Authorization, business-filter resolution and
        # narrowing all complete while the state is still PARSING so a directory
        # ambiguity can legitimately become a clarification (no illegal
        # AUTHORIZING -> CLARIFYING transition) and every denial path happens
        # before any business-data call.
        decision_context = authorization.tenant_context
        scope = authorization.data_scope
        try:
            capability = Capability(fr_id_for(str(capability_id)))
        except ValueError:
            async for event in self._fail(state, "capability_unregistered", usage_events):
                yield event
            return

        decision = authorize_capability(capability, decision_context, scope)
        if not decision.allowed:
            async for event in self._reject(
                state, "forbidden", usage_events, role=decision_context.role
            ):
                yield event
            return

        # Resolve user business filters (dept/employee names, order/
        # style/plan codes) from the intent slots against the MES-filtered
        # directory. Every resolution failure happens before any business-data
        # call and never falls back to a broader scope.
        resolved = _EMPTY_BUSINESS_FILTERS
        if self._business_filters is not None:
            try:
                resolved = await self._business_filters.resolve(scope, intent.slots)
            except DirectoryError as exc:
                if exc.code == "ambiguous":
                    async for event in self._clarify_message(state, exc.message, usage_events):
                        yield event
                    return
                async for event in self._reject_message(
                    state, f"filter_{exc.code}", exc.message, usage_events
                ):
                    yield event
                return

        # FR-012 resolves the target employee in-tenant through the MES-filtered
        # EmployeeQuery; the employee enters the interaction with mes_filtered
        # trust and MES decides actual visibility on the wage call.
        # Personal capabilities (FR-001/002/003/004) bind the caller's own uid;
        # management/boss capabilities leave employee_ids unset so MES row-level
        # filtering decides the visible range.
        is_any_employee = capability == Capability.ANY_EMPLOYEE_PAYROLL
        is_personal = capability in {
            Capability.OWN_OUTPUT,
            Capability.OWN_PAYROLL_SUMMARY,
            Capability.OWN_PAYROLL_DETAIL,
            Capability.GROUP_INCOME_RANK,
        }
        try:
            if is_any_employee:
                filters = self._narrower.narrow(
                    scope,
                    dept_ids=resolved.dept_ids,
                    order_ids=resolved.order_codes,
                    style_ids=resolved.style_codes,
                    plan_ids=resolved.plan_codes,
                    tenant_resolved_employee_ids=resolved.employee_ids,
                )
            elif is_personal:
                filters = self._narrower.narrow(
                    scope,
                    employee_ids=scope.employee_ids,
                    dept_ids=resolved.dept_ids,
                    order_ids=resolved.order_codes,
                    style_ids=resolved.style_codes,
                    plan_ids=resolved.plan_codes,
                )
            else:
                filters = self._narrower.narrow(
                    scope,
                    dept_ids=resolved.dept_ids,
                    order_ids=resolved.order_codes,
                    style_ids=resolved.style_codes,
                    plan_ids=resolved.plan_codes,
                    restrict_to_scope_employees=False,
                )
        except FilterRejectionError as exc:
            async for event in self._reject(state, f"filter_{exc.code}", usage_events):
                yield event
            return

        time_range = _time_range(intent)
        if time_range is None:
            async for event in self._fail(state, "time_range_missing", usage_events):
                yield event
            return
        if _exceeds_time_range_limit(time_range, self._time_range_max_days):
            # Customer-confirmed ceiling: at most the past year. Terminate with
            # a friendly notice before any MES call.
            notice = (
                f"时间范围超出上限（近一年）：请查询最近 {self._time_range_max_days} 天以内的数据。"
            )
            async for event in self._reject_message(
                state, "time_range_exceeds_limit", notice, usage_events
            ):
                yield event
            return

        yield await self._phase(state, SessionState.AUTHORIZING, "intent_complete")
        yield await self._phase(state, SessionState.EXECUTING, "authorized")

        try:
            result = await self._runner.run(
                CapabilityRunRequest(
                    capability_id=capability_id,
                    filters=filters,
                    time_range=time_range,
                    role=decision_context.role,
                )
            )
        except ForbiddenError as exc:
            # Executor-level scope rule (e.g. GongziMxQuery 传空查全部仅限老板):
            # surface as a friendly denial, never a generic failure.
            async for event in self._reject_message(
                state, f"forbidden_{exc.code.value}", exc.message, usage_events
            ):
                yield event
            return
        except Exception:
            async for event in self._fail(state, "execution_failed", usage_events):
                yield event
            return

        # Role-consistency safety net (Story 2): judge the MES return AFTER the
        # capability executed and BEFORE anything user-visible is composed. It
        # never re-filters or rewrites rows and never triggers a re-fetch; it
        # only blocks/warns, records, and alerts.
        verdict = self._scope_verdict(result, capability, decision_context, scope)
        if verdict is not None and not verdict.ok:
            finding = verdict.finding
            if finding is not None:
                await self._record_scope_violation(
                    state, capability, verdict, decision_context, scope, len(result.rows)
                )
                if verdict.action is ValidationAction.BLOCK:
                    # Canonical category mirrors the audit event type names:
                    # scope_violation_exact / scope_violation_heuristic.
                    level_suffix = finding.level.value.removesuffix("_hit")
                    category = f"scope_violation_{level_suffix}"
                    async for event in self._terminate(
                        state,
                        InteractionStatus.FAILED,
                        category,
                        usage_events,
                        self._scope_block_text(verdict, decision_context),
                    ):
                        yield event
                    return

        yield await self._phase(state, SessionState.COMPOSING, "execution_complete")

        artifact_id = None
        if self._exporter is not None:
            try:
                outcome = await self._exporter.export(
                    owner=owner,
                    interaction_id=str(state.record.interaction_id),
                    capability_id=capability_id,
                    role=decision_context.role.value,
                    function=str(capability_id),
                    time_range_label=_time_range_label(time_range),
                    result=result,
                )
                artifact_id = outcome.artifact_id
            except Exception:
                # A failed export must never change the answer outcome.
                artifact_id = None

        state.record = replace(state.record, capability_id=capability_id)
        consistency = _consistency_payload(verdict)
        result_event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_RESULT,
            data={
                "capability_id": str(capability_id),
                "columns": list(result.column_names),
                "row_count": len(result.rows),
                "incomplete": result.incomplete,
                "incomplete_reason": result.incomplete_reason,
                "artifact_id": artifact_id,
                **({"consistency": consistency} if consistency is not None else {}),
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
                            "artifact_id": artifact_id,
                            **({"consistency": consistency} if consistency is not None else {}),
                        },
                    ),
                ),
                events=(result_event, terminal),
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield result_event
        yield terminal

    async def _parse(
        self,
        state: _RunState,
        history: tuple[ConversationTurn, ...],
        usage_events: list[UsageEvent],
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
        usage_events: list[UsageEvent],
    ) -> AsyncIterator[SessionEvent]:
        question = clarification_for(intent) or "请补充更多信息。"
        async for event in self._clarify_message(state, question, usage_events):
            yield event

    async def _clarify_message(
        self,
        state: _RunState,
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
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield event
        yield terminal

    async def _phase(self, state: _RunState, target: SessionState, reason: str) -> SessionEvent:
        advanced = self._advance(state.record, target, reason)
        event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_PHASE,
            data={
                "state": target.value,
                "reason": reason,
                "stage": _STAGE_LABELS.get(target, target.value),
                "status": "ok",
                "duration_ms": state.duration_ms(),
            },
        )
        state.record = replace(
            advanced, last_event_sequence=event.sequence, updated_at=self._clock.now()
        )
        await self._store.commit(
            InteractionCommit(
                interaction=state.record,
                events=(event,),
                usage_events=drain_mes_events(),
            )
        )
        return event

    async def _fail(
        self, state: _RunState, category: str, usage_events: list[UsageEvent]
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._terminate(
            state, InteractionStatus.FAILED, category, usage_events, "查询未能完成。"
        ):
            yield event

    async def _reject(
        self,
        state: _RunState,
        category: str,
        usage_events: list[UsageEvent],
        *,
        role: Role | None = None,
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._reject_message(
            state, category, _friendly_rejection(role), usage_events
        ):
            yield event

    async def _reject_message(
        self,
        state: _RunState,
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
        state: _RunState,
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
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield event

    # ------------------------------------------------------------------
    # Role-consistency safety net (Story 2).
    # ------------------------------------------------------------------

    def _scope_verdict(
        self,
        result: CapabilityRunResult,
        capability: Capability,
        context: TenantContext,
        scope: DataScope,
    ) -> ConsistencyVerdict | None:
        """Run the validator when wired; expected range comes only from the
        authoritative token role and bound scope."""
        if self._validator is None:
            return None
        expected = ExpectedRange.from_context(context, scope)
        return self._validator.validate(
            result=result,
            capability=capability,
            expected=expected,
            mode=self._validation_mode,
        )

    async def _record_scope_violation(
        self,
        state: _RunState,
        capability: Capability,
        verdict: ConsistencyVerdict,
        context: TenantContext,
        scope: DataScope,
        row_count: int,
    ) -> None:
        """Record the finding (review table + audit alert + structured log).

        Best-effort only: a storage failure is logged and never changes the
        interaction outcome. No sensitive value ever enters the record — only
        counts, digests, and the readable expected/actual summaries.
        """
        finding = verdict.finding
        if finding is None:
            return
        now = self._clock.now()
        blocked = verdict.action is ValidationAction.BLOCK
        exact = finding.level is ValidationLevel.EXACT_HIT
        interaction_id = str(state.record.interaction_id)
        entry = ScopeViolationRecord(
            violation_id=self._new_id(),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            role=context.role,
            capability_id=str(capability),
            level=finding.level.value,
            mode=self._validation_mode,
            reason_code=finding.code,
            interaction_id=interaction_id,
            expected_range=finding.expected,
            actual_summary=finding.actual,
            row_count=row_count,
            sample_count=finding.sample_count,
            sample_digests=finding.sample_digests,
            created_at=now,
        )
        if self._violations is not None:
            try:
                await self._violations.record(entry)
            except Exception:  # noqa: BLE001 - best-effort review surface
                _logger.opt(exception=True).warning("session.consistency.violation_store_failed")
        if self._audit is not None:
            try:
                await self._audit.record(
                    AuditEvent(
                        event_type=(
                            AuditEventType.SCOPE_VIOLATION_EXACT
                            if exact
                            else AuditEventType.SCOPE_VIOLATION_HEURISTIC
                        ),
                        outcome=(AuditOutcome.DENIED if blocked else AuditOutcome.ALLOWED),
                        capability_id=str(capability),
                        intent_summary=None,
                        scope_fingerprint=None,
                        employee_count=len(scope.employee_ids),
                        dept_count=len(scope.dept_ids),
                        whole_tenant=scope.mes_filtered,
                        tenant_id=str(context.tenant_id),
                        status="blocked" if blocked else "logged",
                        occurred_at=now,
                        request_id=interaction_id,
                    )
                )
            except Exception:  # noqa: BLE001 - audit must not break the pipeline
                _logger.opt(exception=True).warning("session.consistency.audit_failed")
        _logger.bind(
            level=finding.level.value,
            mode=self._validation_mode,
            code=finding.code,
            capability_id=str(capability),
            role=context.role.value,
            tenant_id=str(context.tenant_id),
            action="block" if blocked else "log",
            row_count=row_count,
        ).warning("session.consistency.violation_detected")

    @staticmethod
    def _scope_block_text(verdict: ConsistencyVerdict, context: TenantContext) -> str:
        finding = verdict.finding
        if finding is None:
            return "本次查询未能完成。"
        return finding.reason

    async def _record_history(self, owner: InteractionOwner, state: _RunState) -> None:
        """Persist a normalized, non-sensitive history entry at terminal state.

        Only the parsed intent survives — never the raw question text, work
        numbers, or wage/output amounts. History is ownership-filtered and a
        failure to record history never changes the answer outcome.
        """
        intent = state.last_intent
        if self._personalization is None or intent is None or intent.capability_id is None:
            return
        try:
            capability_id = CapabilityId(fr_id_for(str(intent.capability_id)))
        except ValueError:
            return
        slots = intent.slots
        non_sensitive: dict[str, object] = {
            "time_expression": slots.time_expression,
            "order_codes": list(slots.order_codes),
            "plan_codes": list(slots.plan_codes),
            "style_codes": list(slots.style_codes),
            "dept_names": list(slots.dept_names),
            "employee_names": list(slots.employee_names),
        }
        non_sensitive = {
            key: value for key, value in non_sensitive.items() if value not in (None, [])
        }
        await self._personalization.record_history(
            owner,
            capability_id=capability_id,
            slots=non_sensitive,
            status=state.record.status.value,
            now=self._clock.now(),
        )

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
    ) -> UsageEvent:
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
    started_monotonic: float = 0.0
    last_intent: CapabilityIntent | None = None

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)


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


def _time_range_label(time_range: TimeRange) -> str:
    return f"{time_range.start.date().isoformat()}_{time_range.end.date().isoformat()}"


def _exceeds_time_range_limit(time_range: TimeRange, max_days: int) -> bool:
    """The customer-confirmed ceiling: queries span at most the past year."""
    return (time_range.end - time_range.start) > timedelta(days=max_days)


def _friendly_rejection(role: Role | None) -> str:
    """Friendly denial naming the caller's actual data range (权限不足友好提示)."""
    if role is None:
        return "当前角色暂不支持该查询。"
    data_range = ROLE_DATA_RANGE.get(role)
    if data_range is None:
        return "当前角色暂不支持该查询。"
    return f"当前角色暂不支持该查询。您可查询的范围：{data_range}。"


def _consistency_payload(verdict: ConsistencyVerdict | None) -> dict[str, object] | None:
    """Front-end renderable consistency fields on result/card events.

    Carries only the structured, non-sensitive fields: level, code, reason and
    the readable expected/actual summaries.
    """
    if verdict is None or verdict.finding is None:
        return None
    finding = verdict.finding
    return {
        "level": finding.level.value,
        "code": finding.code,
        "reason": finding.reason,
        "expected": finding.expected,
        "actual": finding.actual,
        "sample_count": finding.sample_count,
        "blocked": verdict.action is ValidationAction.BLOCK,
    }


#: Human-readable stage labels carried on phase events.
_STAGE_LABELS: dict[SessionState, str] = {
    SessionState.PARSING: "解析",
    SessionState.AUTHORIZING: "鉴权",
    SessionState.EXECUTING: "取数",
    SessionState.COMPOSING: "计算",
    SessionState.ANSWERED: "完成",
    SessionState.CLARIFYING: "追问",
    SessionState.FAILED: "失败",
    SessionState.CANCELLED: "取消",
}


__all__ = [
    "InteractionNotFoundError",
    "SessionLimits",
    "SessionService",
    "StartRequest",
]
