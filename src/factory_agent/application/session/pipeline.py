"""Session run pipeline: parse, authorize, execute, compose, persist."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from factory_agent.application.authorization import ResolvedAuthorization
from factory_agent.application.business_filters import DirectoryError, ResolvedBusinessFilters
from factory_agent.application.capability_map import (
    CHITCHAT_CAPABILITY_ID,
    FR_INFO,
    fr_id_for,
)
from factory_agent.application.consistency import ConsistencyVerdict, ValidationAction
from factory_agent.application.context import ConversationTurn
from factory_agent.application.filters import FilterRejectionError, NarrowedFilters
from factory_agent.application.intent import IntentParseOutcome
from factory_agent.application.permission_matrix import Capability, authorize_capability
from factory_agent.application.scope_guard import (
    ScopeClassification,
    scope_classification_payload,
)
from factory_agent.application.session.consistency import SessionConsistencyMixin
from factory_agent.application.session.definitions import (
    EMPTY_BUSINESS_FILTERS,
    TERMINAL_STATUSES,
    DrillPayload,
    RunState,
    session_logger,
)
from factory_agent.application.session.executor import InteractionRunExecutor
from factory_agent.application.structured import StructuredOutputError
from factory_agent.application.summary import fallback_result_answer, format_aggregate_value
from factory_agent.application.time_expressions import (
    TimeExpressionError,
    resolve_time_expression,
    time_range_violation,
)
from factory_agent.application.usage import (
    drain_mes_events,
    llm_call_event,
    set_usage_context,
)
from factory_agent.domain import (
    INTERACTION_RESULT,
    INTERACTION_STARTED,
    CapabilityId,
    CapabilityIntent,
    DataScope,
    DeptId,
    EmployeeId,
    IntentSlots,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    MessageRole,
    Role,
    SessionEvent,
    SessionState,
    TenantContext,
    TimeRange,
    terminal_event_name,
)
from factory_agent.domain.errors import ForbiddenError
from factory_agent.ports import (
    CapabilityRunRequest,
    CapabilityRunResult,
    InteractionCommit,
    InteractionOwner,
    ModelGatewayError,
    ModelStage,
    TrustedCredential,
    UsageEvent,
)


def time_range_from_intent(intent: CapabilityIntent) -> TimeRange | None:
    start = intent.slots.time_range_start
    end = intent.slots.time_range_end
    if start is None or end is None:
        return None
    return TimeRange(start=start, end=end)


def inclusive_end_day(end: datetime, zone: ZoneInfo) -> date:
    """The inclusive last local calendar day of a half-open [.., end) window.

    Windows are stored in UTC; display and MES ``datee`` share the same
    inclusive-end semantics, so ``end`` is converted to the factory timezone
    and pulled back one microsecond before taking ``.date()`` (taking the
    UTC ``.date()`` shifted labels one day earlier, 2026-09-16 实测).
    """
    return (end.astimezone(zone) - timedelta(microseconds=1)).date()


def time_range_label(time_range: TimeRange, zone: ZoneInfo) -> str:
    """Export filename label: inclusive factory-local ``from_to`` dates."""
    start_day = time_range.start.astimezone(zone).date()
    end_day = inclusive_end_day(time_range.end, zone)
    return f"{start_day.isoformat()}_{end_day.isoformat()}"


def result_aggregates(result: CapabilityRunResult) -> list[tuple[str, str]]:
    """Human-readable totals for the composed answer: label + formatted value.

    Totals are the same pre-aggregated numbers the result card and the XLSX
    合计 row show; no row-level detail and no identifier enters the answer.
    """
    titles = result.column_titles or {}
    units = result.column_units or {}
    types = result.column_types or {}
    aggregates: list[tuple[str, str]] = []
    for name in result.column_names:
        value = result.totals.get(name)
        if value is None:
            continue
        label = titles.get(name, name)
        unit = units.get(name)
        if unit:
            label = f"{label}（{unit}）"
        aggregates.append((label, format_aggregate_value(value, types.get(name))))
    return aggregates


def consistency_payload(verdict: ConsistencyVerdict | None) -> dict[str, object] | None:
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


@dataclass
class _ExecutionPlan:
    """Everything the pre-execution authorization chain resolves.

    Filled atomically on success by ``_authorize_request``; every field stays
    ``None`` when the chain emitted a failure/clarification/denial instead.
    """

    capability_id: CapabilityId | None = None
    capability: Capability | None = None
    decision_context: TenantContext | None = None
    scope: DataScope | None = None
    filters: NarrowedFilters | None = None
    time_range: TimeRange | None = None


class SessionPipelineMixin(SessionConsistencyMixin):
    """The bounded conversation pipeline from claim to durable terminal."""

    async def _run(
        self,
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        record: InteractionRecord,
        history: tuple[ConversationTurn, ...],
        after_sequence: int,
        credential: TrustedCredential,
        control: InteractionRunExecutor | None = None,
        drill: DrillPayload | None = None,
    ) -> AsyncIterator[SessionEvent]:
        state = RunState(
            record=record,
            sequence=max(record.last_event_sequence, after_sequence),
            started_monotonic=time.monotonic(),
        )
        usage_events: list[UsageEvent] = []
        # The adapter meters every MES call at its ``_send`` exit; the active
        # usage context is bound for the duration of this run so business-data
        # calls carry the interaction identifiers. The credential binder scopes
        # every MES call in this run to the caller's own token bundle. Both
        # live in ContextVars set inside the executor task's own context
        # (``asyncio.create_task`` copies it), so concurrent connections and
        # the spawning request handler are never affected.
        set_usage_context(self._usage_context(record))
        binder = self._credential_binder
        binding = binder.bind_for(credential) if binder is not None else nullcontext()
        try:
            with binding:
                # Cooperative stop point before any work: a cancel persisted
                # between the claim and this point must not be resurrected by
                # the ``started`` commit below.
                stop = await self._stop_reason(owner, record.interaction_id, control)
                if stop is not None:
                    return
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
                await self._commit(
                    InteractionCommit(
                        interaction=state.record,
                        events=(started,),
                        usage_events=drain_mes_events(),
                    )
                )
                yield started

                try:
                    async for event in self._pipeline(
                        owner, authorization, state, history, usage_events, control, drill=drill
                    ):
                        yield event
                    if state.record.status in TERMINAL_STATUSES:
                        await self._record_history(owner, state)
                except asyncio.CancelledError:
                    # Shutdown drain only: the connection never cancels this
                    # task. The interaction stays ``running`` and the next
                    # startup sweep (or a follower's stale check) marks it
                    # ``executor_lost``.
                    raise
                except Exception:
                    # Last-resort net for a crash outside the pipeline's own
                    # error handling (e.g. a store failure): durably fail the
                    # interaction so no connection ever tails a dead run.
                    session_logger.exception(
                        "session.executor.pipeline_crashed",
                        interaction_id=str(state.record.interaction_id),
                    )
                    async for event in self._terminate(
                        state,
                        InteractionStatus.FAILED,
                        "executor_lost",
                        usage_events,
                        "查询未能完成。",
                    ):
                        yield event
        finally:
            set_usage_context(None)

    async def _authorize_request(
        self,
        authorization: ResolvedAuthorization,
        state: RunState,
        intent: CapabilityIntent,
        rewrite_query: str | None,
        capability_id: CapabilityId,
        usage_events: list[UsageEvent],
        plan: _ExecutionPlan,
        scope_classification: ScopeClassification | None = None,
        drill: DrillPayload | None = None,
    ) -> AsyncIterator[SessionEvent]:
        """Run every denial path before any business-data call.

        Fills ``plan``, announcing the scope-resolution progress event on the
        way; otherwise yields the failure / clarification / denial events and
        leaves ``plan`` untouched. The scope decision comes from
        ``scope_classification`` when the EXTRACT call produced a usable one,
        and from the dedicated guard call otherwise.
        """
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

        # Pre-execution scope check: a question can name a target that is
        # outside the caller's range while still mapping to an allowed
        # capability (e.g. an employee asking for the whole group's wage
        # detail maps to FR-003 and would silently come back as their own
        # rows). Deny before any business-data call; the check can only narrow
        # access, never grant it. The merged EXTRACT payload classifies the
        # scope in the same call when it produced a usable verdict; a missing
        # or invalid key falls through to the dedicated call, which keeps the
        # fail-open availability promise (a model failure never blocks the run).
        guard = self._scope_guard
        if scope_classification is not None:
            denial = self._merged_scope_denial(state, scope_classification, decision_context.role)
        elif guard is not None:
            question = rewrite_query or state.record.input_text
            denial = await self._run_scope_guard(
                state,
                guard,
                question,
                capability,
                decision_context.role,
                usage_events,
            )
        else:
            denial = None
        if denial is not None:
            async for event in self._reject_message(state, "scope_forbidden", denial, usage_events):
                yield event
            return

        # Resolve user business filters (dept/employee names, order/
        # style/plan codes) from the intent slots against the MES-filtered
        # directory. Every resolution failure happens before any business-data
        # call and never falls back to a broader scope. A structured drill
        # round skips name resolution entirely: it already carries validated
        # ids, which only go through the same scope-narrowing gates below.
        resolved = EMPTY_BUSINESS_FILTERS
        if drill is not None:
            try:
                resolved = await self._resolve_drill_filters(drill, scope)
            except DirectoryError as exc:
                async for event in self._reject_message(
                    state, f"filter_{exc.code}", exc.message, usage_events
                ):
                    yield event
                return
        elif self._business_filters is not None:
            # Directory lookups (dept/employee names) read the MES-filtered
            # directory and can take a while; announce them before the first
            # one — but only when a slot actually names a department or
            # employee. A personal query whose slots carry no names resolves
            # nothing, and announcing it would be a misleading front-end hint.
            if intent.slots.dept_names or intent.slots.employee_names:
                yield await self._progress(state, "scope_resolution_started")
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
                    material_ids=resolved.material_ids,
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
                    material_ids=resolved.material_ids,
                )
            else:
                filters = self._narrower.narrow(
                    scope,
                    dept_ids=resolved.dept_ids,
                    order_ids=resolved.order_codes,
                    style_ids=resolved.style_codes,
                    plan_ids=resolved.plan_codes,
                    material_ids=resolved.material_ids,
                    restrict_to_scope_employees=False,
                )
        except FilterRejectionError as exc:
            async for event in self._reject(state, f"filter_{exc.code}", usage_events):
                yield event
            return

        time_range = time_range_from_intent(intent)
        if time_range is None:
            async for event in self._fail(state, "time_range_missing", usage_events):
                yield event
            return
        # The customer-confirmed ceiling (past year) is judged by the same
        # shared redline the parser uses for a model-proposed range, so the
        # span rule is written once.
        if (
            time_range_violation(
                time_range.start,
                time_range.end,
                max_days=self._time_range_max_days,
            )
            == "too_long"
        ):
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

        plan.capability_id = capability_id
        plan.capability = capability
        plan.decision_context = decision_context
        plan.scope = scope
        plan.filters = filters
        plan.time_range = time_range

    async def _resolve_drill_filters(
        self, drill: DrillPayload, scope: DataScope
    ) -> ResolvedBusinessFilters:
        """Turn structured drill slots into narrowing inputs (D-3/D-5 拍板).

        Drill slots are already ids, not names. The department set goes through
        the normal ``FilterNarrower`` scope intersection later (out-of-range
        depts are rejected before any business call). The target employee of a
        drill round is looked up in the tenant directory, and for non-
        whole-tenant scopes (01/02) must sit inside the caller's bound
        department set — server-side narrowing per D-5; MES row-level
        filtering stays the second line of defence.
        """
        dept_ids: frozenset[DeptId] | None = None
        if drill.dept_ids:
            if not scope.mes_filtered:
                out_of_range = [
                    value for value in drill.dept_ids if DeptId(value) not in scope.dept_ids
                ]
                if out_of_range:
                    raise DirectoryError("forbidden", "请求的车间/小组不在您可查询的范围内。")
            dept_ids = frozenset(DeptId(value) for value in drill.dept_ids)
        employee_ids: frozenset[EmployeeId] | None = None
        if drill.employee_uid:
            if self._business_filters is None:
                raise DirectoryError("not_found", "员工目录不可用，无法下钻查询该员工。")
            employee = await self._business_filters.find_employee_by_id(scope, drill.employee_uid)
            if employee is None:
                raise DirectoryError("not_found", "未找到该员工，无法下钻查询。")
            if not scope.mes_filtered:
                employee_dept = DeptId(employee.dept) if employee.dept is not None else None
                if employee_dept is None or employee_dept not in scope.dept_ids:
                    raise DirectoryError("forbidden", "该员工不在您可查询的车间范围内。")
            employee_ids = frozenset({EmployeeId(drill.employee_uid)})
        return ResolvedBusinessFilters(
            employee_ids=employee_ids,
            dept_ids=dept_ids,
            order_codes=None,
            style_codes=None,
            plan_codes=None,
            material_ids=None,
        )

    async def _pipeline(
        self,
        owner: InteractionOwner,
        authorization: ResolvedAuthorization,
        state: RunState,
        history: tuple[ConversationTurn, ...],
        usage_events: list[UsageEvent],
        control: InteractionRunExecutor | None = None,
        drill: DrillPayload | None = None,
    ) -> AsyncIterator[SessionEvent]:
        stop = await self._stop_reason(owner, state.record.interaction_id, control)
        if stop is not None:
            async for event in self._stop_here(state, usage_events, stop):
                yield event
            return
        # The parse call stays silent for as long as the model takes; announce
        # the stage before it starts so the caller watches progress, not a stall.
        yield await self._progress(state, "parse_started")
        if drill is not None:
            # Structured drill (D-3 拍板)：intent comes from the validated
            # drill payload, not from a model call — zero LLM spend, zero
            # routing ambiguity. Scope classification is likewise
            # deterministic (the slots were validated against the DataScope
            # below, in the authorization chain).
            session_logger.info(
                "session.drill.applied",
                capability_id=drill.capability_id,
                dept_count=len(drill.dept_ids),
                has_employee=drill.employee_uid is not None,
                interaction_id=str(state.record.interaction_id),
                session_id=str(state.record.session_id),
            )
            try:
                parsed = self._drill_parse_outcome(drill)
            except TimeExpressionError:
                async for event in self._reject_message(
                    state,
                    "drill_invalid_time",
                    "下钻时间范围无法识别，请重新发起查询。",
                    usage_events,
                ):
                    yield event
                return
            # The drill round still gets its EXTRACT metering record (审计与
            # 计量打通), marked as the deterministic no-model path so usage
            # dashboards can separate it from real model spend.
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=self._clock.now(),
                    logical_call_id=self._new_id(),
                    stage=ModelStage.EXTRACT,
                    model_alias="structured-drill",
                    actual_model="structured_drill",
                    attempt=0,
                    duration_ms=0,
                    status="completed",
                    includes_scope=False,
                )
            )
        else:
            try:
                parsed = await self._parse(
                    state, history, usage_events, role=authorization.tenant_context.role
                )
            except ModelGatewayError as exc:
                async for event in self._fail(state, f"gateway_{exc.category.value}", usage_events):
                    yield event
                return
            except StructuredOutputError:
                async for event in self._fail(state, "model_output_invalid", usage_events):
                    yield event
                return

        intent = parsed.intent
        state.last_intent = intent

        if intent.needs_clarification:
            if state.record.clarification_rounds + 1 >= self._limits.max_clarification_rounds:
                async for event in self._fail(state, "clarification_exhausted", usage_events):
                    yield event
                return
            async for event in self._clarify(
                state, intent, usage_events, rewrite_query=parsed.rewrite_query
            ):
                yield event
            return

        # Chit-chat is resolved by the same capability selector but never becomes
        # a business capability: it is intercepted here, before the permission
        # matrix or any MES call. When the merged parse call already produced a
        # reply (``content``) it is answered directly with zero extra LLM calls;
        # the dedicated ChatResponder remains a fallback for empty/legacy
        # payloads. A chit-chat utterance that is low-confidence/ambiguous is
        # clarified first instead of being answered.
        if intent.capability_id is not None and str(intent.capability_id) == CHITCHAT_CAPABILITY_ID:
            if parsed.content:
                async for event in self._chat_with_text(state, parsed.content, usage_events):
                    yield event
            else:
                async for event in self._chat(state, history, usage_events):
                    yield event
            return

        capability_id = intent.capability_id
        if capability_id is None:
            async for event in self._fail(state, "capability_unresolved", usage_events):
                yield event
            return

        # The chain judges the role, runs the scope-guard model call, and
        # resolves the caller's directory names before any business call:
        # announce it up front so none of that is a black box.
        yield await self._progress(state, "authorize_started")
        plan = _ExecutionPlan()
        async for event in self._authorize_request(
            authorization,
            state,
            intent,
            parsed.rewrite_query,
            capability_id,
            usage_events,
            plan,
            parsed.scope_verdict,
            drill=drill,
        ):
            yield event
        if (
            plan.capability is None
            or plan.decision_context is None
            or plan.scope is None
            or plan.filters is None
            or plan.time_range is None
        ):
            return
        capability = plan.capability
        decision_context = plan.decision_context
        scope = plan.scope
        filters = plan.filters
        time_range = plan.time_range

        # Cooperative stop point before the business-data call: a
        # cancel or an exhausted wall-clock budget ends the run before any
        # further MES spend.
        stop = await self._stop_reason(owner, state.record.interaction_id, control)
        if stop is not None:
            async for event in self._stop_here(state, usage_events, stop):
                yield event
            return

        for event in await self._phases(
            state,
            (SessionState.AUTHORIZING, "intent_complete"),
            (SessionState.EXECUTING, "authorized"),
        ):
            yield event

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
            # Executor-level scope rule: surface as a friendly denial, never a
            # generic failure.
            async for event in self._reject_message(
                state, f"forbidden_{exc.code.value}", exc.message, usage_events
            ):
                yield event
            return
        except Exception:
            session_logger.exception(
                "interaction.execution_failed",
                interaction_id=str(state.record.interaction_id),
                capability_id=str(capability_id),
            )
            async for event in self._fail(state, "execution_failed", usage_events):
                yield event
            return

        # Role-consistency safety net: judge the MES return AFTER the
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

        # Cooperative stop point before compose: the last chance to
        # honor a cancel or the wall-clock budget before the answer is built.
        stop = await self._stop_reason(owner, state.record.interaction_id, control)
        if stop is not None:
            async for event in self._stop_here(state, usage_events, stop):
                yield event
            return

        async for event in self._complete_result(
            owner,
            state,
            result,
            capability_id,
            decision_context.role,
            time_range,
            verdict,
            usage_events,
        ):
            yield event

    async def _complete_result(
        self,
        owner: InteractionOwner,
        state: RunState,
        result: CapabilityRunResult,
        capability_id: CapabilityId,
        role: Role,
        time_range: TimeRange,
        verdict: ConsistencyVerdict | None,
        usage_events: list[UsageEvent],
    ) -> AsyncIterator[SessionEvent]:
        """Compose the answer, export the artifact, persist the completed turn.

        A failed export never changes the outcome; the result card message
        and the terminal event commit atomically with the usage events.
        """
        yield await self._phase(state, SessionState.COMPOSING, "execution_complete")

        answer_text = await self._compose_result_answer(
            state, result, capability_id, time_range, usage_events
        )

        artifact_id = None
        if self._exporter is not None:
            try:
                outcome = await self._exporter.export(
                    owner=owner,
                    interaction_id=str(state.record.interaction_id),
                    capability_id=capability_id,
                    role=role.value,
                    function=str(capability_id),
                    time_range_label=time_range_label(time_range, self._factory_zone),
                    result=result,
                )
                artifact_id = outcome.artifact_id
            except Exception:  # noqa: BLE001 - a failed export must never change the answer outcome
                # Degrades to "no export button this time" (artifact_id=None);
                # the warning makes an unwritable store or renderer fault
                # diagnosable instead of a silently missing button.
                session_logger.opt(exception=True).warning(
                    "session.export.failed",
                    interaction_id=str(state.record.interaction_id),
                )
                artifact_id = None

        state.record = replace(state.record, capability_id=capability_id)
        consistency = consistency_payload(verdict)
        column_titles = [
            (result.column_titles or {}).get(name, name) for name in result.column_names
        ]
        # The card payload (recipe-declared, built by the kernel) is placed on
        # the SSE event and the persisted result_table message as the SAME
        # dict, so live, replay and history streams render identically.
        card_payload: dict[str, object] = (
            {"card": result.card} if getattr(result, "card", None) is not None else {}
        )
        result_event = SessionEvent(
            sequence=state.next_sequence(),
            name=INTERACTION_RESULT,
            data={
                "capability_id": str(capability_id),
                "columns": list(result.column_names),
                "column_titles": column_titles,
                "row_count": len(result.rows),
                "incomplete": result.incomplete,
                "incomplete_reason": result.incomplete_reason,
                "artifact_id": artifact_id,
                "answer": answer_text,
                **card_payload,
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
        # Final user-visible outcome on the result path: structured metadata
        # only — capability, columns, row_count and completeness. Raw result
        # rows are deliberately never logged (sensitive business values).
        session_logger.info(
            "session.outcome.result capability={capability_id} rows={row_count} "
            "incomplete={incomplete} reason={incomplete_reason} "
            "artifact={artifact_id} columns=[{columns}] answer={answer}",
            capability_id=str(capability_id),
            row_count=len(result.rows),
            incomplete=result.incomplete,
            incomplete_reason=result.incomplete_reason,
            artifact_id=artifact_id,
            columns=",".join(result.column_names),
            answer=answer_text,
            interaction_id=str(state.record.interaction_id),
            session_id=str(state.record.session_id),
        )
        usage_events.append(
            self._completion_event(
                state.record,
                result=result,
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
                        MessageKind.RESULT_TABLE,
                        result_event.sequence,
                        f"已返回 {len(result.rows)} 行结果。",
                        payload={
                            "capability_id": str(capability_id),
                            "columns": list(result.column_names),
                            "column_titles": column_titles,
                            "row_count": len(result.rows),
                            "incomplete": result.incomplete,
                            "incomplete_reason": result.incomplete_reason,
                            "artifact_id": artifact_id,
                            "answer": answer_text,
                            **card_payload,
                            **({"consistency": consistency} if consistency is not None else {}),
                        },
                    ),
                    self._message(
                        state.record,
                        MessageRole.ASSISTANT,
                        MessageKind.PLAIN_TEXT,
                        terminal.sequence,
                        answer_text,
                    ),
                ),
                events=(result_event, terminal),
                usage_events=tuple(usage_events) + drain_mes_events(),
            )
        )
        yield result_event
        yield terminal

    def _drill_parse_outcome(self, drill: DrillPayload) -> IntentParseOutcome:
        """Synthetic parse outcome for a structured drill round.

        Time comes from the reviewed expression vocabulary (default 当月);
        confidence is 1.0 because the capability is client-declared from a
        server-authored card action, and the slots carry no ambiguity.
        """
        expression = drill.time_expression or "本月"
        time_range = resolve_time_expression(
            expression, self._clock.now(), self._factory_timezone_name
        )
        intent = CapabilityIntent(
            capability_id=CapabilityId(drill.capability_id),
            confidence=1.0,
            slots=IntentSlots(
                time_range_start=time_range.start,
                time_range_end=time_range.end,
                time_expression=expression,
            ),
        )
        return IntentParseOutcome(
            intent=intent,
            clarification=None,
            attempts=0,
            actual_model="structured_drill",
            duration_ms=0,
            rewrite_query=None,
            scope_verdict=ScopeClassification(beyond=False, target="structured drill"),
        )

    async def _parse(
        self,
        state: RunState,
        history: tuple[ConversationTurn, ...],
        usage_events: list[UsageEvent],
        *,
        role: Role,
    ) -> IntentParseOutcome:
        logical_call_id = self._new_id()
        now = self._clock.now()
        try:
            outcome = await self._parser.parse(
                state.record.input_text,
                now=now,
                logical_call_id=logical_call_id,
                history=history,
                role=role,
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
                    includes_scope=self._parser_includes_scope(role),
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
                includes_scope=outcome.includes_scope,
                scope_verdict=(
                    scope_classification_payload(outcome.scope_verdict)
                    if outcome.scope_verdict is not None
                    else None
                ),
            )
        )
        return outcome

    def _parser_includes_scope(self, role: Role) -> bool:
        """Whether the parser asks its EXTRACT call for a scope classification.

        Mirrors the parser's own rule so the failed-attempt event reports the
        same shape as a successful one.
        """
        return self._parser.includes_scope_for(role)

    async def _chat(
        self,
        state: RunState,
        history: tuple[ConversationTurn, ...],
        usage_events: list[UsageEvent],
    ) -> AsyncIterator[SessionEvent]:
        """Fallback chit-chat generation: one dedicated CHAT model call.

        Used only when the merged intent call carried no usable ``content``
        (legacy or empty reply). Still zero business calls.
        """
        responder = self._chat_responder
        if responder is None:
            async for event in self._fail(state, "capability_unresolved", usage_events):
                yield event
            return
        logical_call_id = self._new_id()
        now = self._clock.now()
        try:
            reply = await responder.reply(
                state.record.input_text,
                logical_call_id=logical_call_id,
                history=history,
            )
        except ModelGatewayError as exc:
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=now,
                    logical_call_id=logical_call_id,
                    stage=ModelStage.CHAT,
                    model_alias=responder.model_alias,
                    actual_model="unknown",
                    attempt=exc.attempt,
                    duration_ms=exc.duration_ms,
                    status="failed",
                    error_category=exc.category.value,
                )
            )
            async for event in self._fail(state, f"gateway_{exc.category.value}", usage_events):
                yield event
            return
        except StructuredOutputError:
            async for event in self._fail(state, "model_output_invalid", usage_events):
                yield event
            return
        usage_events.append(
            llm_call_event(
                self._usage_context(state.record),
                occurred_at=now,
                logical_call_id=logical_call_id,
                stage=ModelStage.CHAT,
                model_alias=reply.model_alias,
                actual_model=reply.actual_model,
                attempt=reply.attempt,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                cached_tokens=reply.cached_tokens,
                reasoning_tokens=reply.reasoning_tokens,
                duration_ms=reply.duration_ms,
                status="completed",
            )
        )
        async for event in self._chat_with_text(state, reply.text, usage_events):
            yield event

    async def _compose_result_answer(
        self,
        state: RunState,
        result: CapabilityRunResult,
        capability_id: CapabilityId,
        time_range: TimeRange,
        usage_events: list[UsageEvent],
    ) -> str:
        """One composed answer sentence over result metadata and totals.

        Row-level detail never enters the prompt (sensitive-field invariant);
        the model narrates from metadata and the pre-aggregated totals that
        are already rendered on the result card. On any model failure the
        deterministic fallback keeps the outcome intact: a failed answer
        never fails the interaction.
        """
        time_label = self._result_time_label(state, time_range, self._factory_zone)
        aggregates = result_aggregates(result)
        fallback = fallback_result_answer(
            row_count=len(result.rows),
            incomplete=result.incomplete,
            incomplete_reason=result.incomplete_reason,
            time_label=time_label,
            aggregates=aggregates,
        )
        summarizer = self._summarizer
        if summarizer is None:
            return fallback
        logical_call_id = self._new_id()
        now = self._clock.now()
        fr_id = fr_id_for(str(capability_id))
        title = FR_INFO.get(fr_id, (fr_id, ""))[0]
        try:
            reply = await summarizer.summarize(
                question=state.record.input_text,
                capability_title=title,
                time_label=time_label,
                row_count=len(result.rows),
                columns=list(result.column_names),
                incomplete=result.incomplete,
                incomplete_reason=result.incomplete_reason,
                logical_call_id=logical_call_id,
                aggregates=aggregates,
            )
        except ModelGatewayError as exc:
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=now,
                    logical_call_id=logical_call_id,
                    stage=ModelStage.SUMMARIZE,
                    model_alias=summarizer.model_alias,
                    actual_model="unknown",
                    attempt=exc.attempt,
                    duration_ms=exc.duration_ms,
                    status="failed",
                    error_category=exc.category.value,
                )
            )
            return fallback
        except StructuredOutputError as exc:
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=now,
                    logical_call_id=logical_call_id,
                    stage=ModelStage.SUMMARIZE,
                    model_alias=summarizer.model_alias,
                    actual_model="unknown",
                    attempt=exc.attempts,
                    status="failed",
                    error_category="protocol",
                )
            )
            return fallback
        usage_events.append(
            llm_call_event(
                self._usage_context(state.record),
                occurred_at=now,
                logical_call_id=logical_call_id,
                stage=ModelStage.SUMMARIZE,
                model_alias=reply.model_alias,
                actual_model=reply.actual_model,
                attempt=reply.attempt,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                cached_tokens=reply.cached_tokens,
                reasoning_tokens=reply.reasoning_tokens,
                duration_ms=reply.duration_ms,
                status="completed",
            )
        )
        return reply.text

    @staticmethod
    def _result_time_label(state: RunState, time_range: TimeRange, zone: ZoneInfo) -> str:
        """Human time label for the answer: the caller's own words when present."""
        expression = state.last_intent.slots.time_expression if state.last_intent else None
        if expression:
            return expression
        start_day = time_range.start.astimezone(zone).date()
        end_day = inclusive_end_day(time_range.end, zone)
        return f"{start_day.isoformat()} 至 {end_day.isoformat()}"

    async def _record_history(self, owner: InteractionOwner, state: RunState) -> None:
        """Persist a normalized, non-sensitive history entry at terminal state.

        Only the parsed intent survives — never the raw question text, work
        numbers, or wage/output amounts. History is ownership-filtered and a
        failure to record history never changes the answer outcome.
        """
        intent = state.last_intent
        if self._personalization is None or intent is None or intent.capability_id is None:
            return
        if str(intent.capability_id) == CHITCHAT_CAPABILITY_ID:
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
