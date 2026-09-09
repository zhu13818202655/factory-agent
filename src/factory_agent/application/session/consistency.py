"""Role-consistency safety net and pre-execution scope guard for sessions."""

from factory_agent.application.capability_map import FR_INFO
from factory_agent.application.consistency import (
    ConsistencyVerdict,
    ValidationAction,
    ValidationLevel,
)
from factory_agent.application.permission_matrix import Capability
from factory_agent.application.scope_guard import ScopeGuard, ScopeVerdict, deny_message
from factory_agent.application.session.definitions import RunState, session_logger
from factory_agent.application.session.outcomes import SessionOutcomeMixin
from factory_agent.application.structured import StructuredOutputError
from factory_agent.application.usage import llm_call_event
from factory_agent.domain import (
    DataScope,
    ExpectedRange,
    Role,
    TenantContext,
)
from factory_agent.observability.audit import AuditEvent, AuditEventType, AuditOutcome
from factory_agent.ports import (
    CapabilityRunResult,
    ModelGatewayError,
    ModelStage,
    UsageEvent,
)
from factory_agent.ports.scope_violation import ScopeViolationRecord


class SessionConsistencyMixin(SessionOutcomeMixin):
    """Scope guard (deny-before-call) and post-fetch consistency validation."""

    async def _run_scope_guard(
        self,
        state: RunState,
        guard: ScopeGuard,
        question: str,
        capability: Capability,
        role: Role,
        usage_events: list[UsageEvent],
    ) -> str | None:
        """User-facing denial message when the request exceeds the caller's
        authorized range; ``None`` when the request is within range or the
        guard fails open.

        The guard only narrows access: the authoritative bounds stay the
        token role matrix and MES row filtering, so a model failure never
        blocks the run — it skips the friendly denial and leaves MES
        filtering in charge of every row.
        """
        title = FR_INFO.get(capability.value, (capability.value, ""))[0]
        logical_call_id = self._new_id()
        now = self._clock.now()
        try:
            verdict: ScopeVerdict = await guard.check(
                question=question,
                capability_title=title,
                role=role,
                logical_call_id=logical_call_id,
            )
        except ModelGatewayError as exc:
            usage_events.append(
                llm_call_event(
                    self._usage_context(state.record),
                    occurred_at=now,
                    logical_call_id=logical_call_id,
                    stage=ModelStage.SCOPE_GUARD,
                    model_alias=guard.model_alias,
                    actual_model="unknown",
                    attempt=exc.attempt,
                    duration_ms=exc.duration_ms,
                    status="failed",
                    error_category=exc.category.value,
                )
            )
            session_logger.warning(
                "session.scope_guard.failed_open category={category}",
                category=exc.category.value,
                interaction_id=str(state.record.interaction_id),
                session_id=str(state.record.session_id),
            )
            return None
        except StructuredOutputError as exc:
            session_logger.warning(
                "session.scope_guard.failed_open category=model_output_invalid attempts={attempts}",
                attempts=exc.attempts,
                interaction_id=str(state.record.interaction_id),
                session_id=str(state.record.session_id),
            )
            return None
        usage_events.append(
            llm_call_event(
                self._usage_context(state.record),
                occurred_at=now,
                logical_call_id=logical_call_id,
                stage=ModelStage.SCOPE_GUARD,
                model_alias=verdict.model_alias,
                actual_model=verdict.actual_model,
                attempt=verdict.attempt,
                prompt_tokens=verdict.prompt_tokens,
                completion_tokens=verdict.completion_tokens,
                cached_tokens=verdict.cached_tokens,
                reasoning_tokens=verdict.reasoning_tokens,
                duration_ms=verdict.duration_ms,
                status="completed",
            )
        )
        session_logger.info(
            "session.scope_guard.verdict capability={capability} beyond={beyond} target={target}",
            capability=capability.value,
            beyond=verdict.beyond,
            target=verdict.target,
            interaction_id=str(state.record.interaction_id),
            session_id=str(state.record.session_id),
        )
        if not verdict.beyond:
            return None
        return deny_message(role, verdict.target)

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
        state: RunState,
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
                session_logger.opt(exception=True).warning(
                    "session.consistency.violation_store_failed"
                )
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
                session_logger.opt(exception=True).warning("session.consistency.audit_failed")
        session_logger.bind(
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
