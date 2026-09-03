from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
    IdentityRejectionError,
)
from factory_agent.application.business_filters import (
    BusinessFilterResolver,
    DeptRecord,
    DirectoryError,
    EmployeeRecord,
)
from factory_agent.application.context import ConversationTurn
from factory_agent.application.intent import CapabilityCatalog, CapabilitySpec
from factory_agent.application.session import (
    InteractionNotFoundError,
    SessionLimits,
    SessionService,
    StartRequest,
)
from factory_agent.application.usage import pseudonymous_subject
from factory_agent.domain import (
    INTERACTION_CLARIFICATION,
    INTERACTION_RESULT,
    INTERACTION_STARTED,
    CapabilityId,
    DataScope,
    EmployeeId,
    InteractionId,
    InteractionStatus,
    Role,
    SessionEvent,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.ports import InteractionOwner, ModelErrorCategory, ModelGatewayError
from factory_agent.ports.contracts import TrustedCredential
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
    SequentialIds,
)

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
SESSION = SessionId("session-1")
CANARY = "员工 E-CANARY 上月工资 8213.44 元"

CATALOG = CapabilityCatalog(
    specs=(
        CapabilitySpec(
            capability_id=CapabilityId("FR-001"),
            title="查看本人产量",
            required_slots=("time_range",),
        ),
        CapabilitySpec(
            capability_id=CapabilityId("FR-011"),
            title="全厂计件统计",
            required_slots=("time_range",),
        ),
        CapabilitySpec(
            capability_id=CapabilityId("FR-012"),
            title="任一员工工资查询",
            required_slots=("time_range", "employee_names"),
        ),
    )
)

INTENT_PAYLOAD = (
    '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
)
OWNER_ONLY_PAYLOAD = (
    '{"capability_id": "FR-011", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
)
ANY_EMPLOYEE_PAYLOAD = (
    '{"capability_id": "FR-012", "confidence": 0.95, "slots": '
    '{"time_expression": "上个月", "employee_names": ["模拟员工甲"]}}'
)
INCOMPLETE_PAYLOAD = '{"capability_id": null, "confidence": 0.2, "slots": {}}'
#: Explicit slot dates spanning more than the one-year ceiling (客户确认 2).
TOO_WIDE_PAYLOAD = (
    '{"capability_id": "FR-001", "confidence": 0.95, "slots": '
    '{"time_range_start": "2025-01-01T00:00:00+00:00", '
    '"time_range_end": "2026-08-01T00:00:00+00:00"}}'
)


class FakeDirectory:
    def __init__(
        self,
        *,
        dept_error: DirectoryError | None = None,
        employee_error: DirectoryError | None = None,
    ) -> None:
        self._dept_error = dept_error
        self._employee_error = employee_error

    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]:
        if self._dept_error is not None:
            raise self._dept_error
        return (DeptRecord("dept-1", "一车间", "YCJ"),)

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]:
        if self._employee_error is not None:
            raise self._employee_error
        return (EmployeeRecord("emp-1", "模拟员工甲", "MNYGJ"),)


def credential(tenant: str = "tenant-a", user: str = "user-a") -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId(tenant), user_id=UserId(user))


def authorization(role: Role = Role.EMPLOYEE) -> AuthorizationService:
    member = membership("user-a", "tenant-a", "emp-1", role)
    return AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={("tenant-a", "user-a"): member}
        ),
        organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
        versions=FixedScopeVersionAssigner(),
    )


def build(
    contents: list[str] | None = None,
    *,
    role: Role = Role.EMPLOYEE,
    failures: list[Exception | None] | None = None,
    runner: RecordingCapabilityRunner | None = None,
    limits: SessionLimits | None = None,
    store: InMemoryInteractionStore | None = None,
    directory: FakeDirectory | None = None,
) -> tuple[SessionService, InMemoryInteractionStore, RecordingCapabilityRunner]:
    from factory_agent.application.intent import CapabilityIntentParser

    gateway = ScriptedModelGateway(contents=contents or [INTENT_PAYLOAD], failures=failures or [])
    parser = CapabilityIntentParser(
        gateway, CATALOG, model_alias="factory-fast", timezone_name="Asia/Shanghai"
    )
    resolved_store = store or InMemoryInteractionStore()
    resolved_runner = runner or RecordingCapabilityRunner()
    service = SessionService(
        resolved_store,
        authorization(role),
        parser,
        resolved_runner,
        FrozenClock(NOW),
        new_id=SequentialIds(),
        limits=limits,
        sleep=_no_sleep,
        business_filters=BusinessFilterResolver(directory or FakeDirectory()),
    )
    return service, resolved_store, resolved_runner


async def _no_sleep(_: float) -> None:
    return None


async def drain(
    service: SessionService,
    interaction_id: InteractionId,
    *,
    after_sequence: int = 0,
    history: tuple[ConversationTurn, ...] = (),
) -> list[SessionEvent]:
    stream = service.stream(
        credential(), interaction_id, after_sequence=after_sequence, history=history
    )
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_start_persists_the_interaction_and_first_message_before_streaming() -> None:
    service, store, runner = build()

    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.PENDING
    assert [message.text for message in store.messages] == ["上个月产量"]
    assert runner.requests == []


@pytest.mark.asyncio
async def test_stream_starts_with_interaction_started_and_one_terminal_event() -> None:
    service, _, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    assert events[0].name == INTERACTION_STARTED
    terminal = [event for event in events if event.name.startswith("interaction.completed")]
    assert len(terminal) == 1
    assert events[-1].name == "interaction.completed"


@pytest.mark.asyncio
async def test_event_sequence_is_monotonic_and_gap_free() -> None:
    service, _, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_successful_run_reaches_the_answered_state_and_emits_a_result() -> None:
    service, store, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    assert any(event.name == INTERACTION_RESULT for event in events)
    stored = store.interactions[str(record.interaction_id)]
    assert stored.state is SessionState.ANSWERED
    assert stored.status is InteractionStatus.COMPLETED
    assert len(runner.requests) == 1


@pytest.mark.asyncio
async def test_executor_only_receives_scope_narrowed_filters() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    await drain(service, record.interaction_id)

    filters = runner.requests[0].filters
    assert filters.tenant_id == TenantId("tenant-a")
    assert filters.employee_ids == frozenset({EmployeeId("emp-1")})


@pytest.mark.asyncio
async def test_owner_capability_runs_for_the_owner_role() -> None:
    """The owner may run the factory-wide payroll capability."""
    service, store, runner = build([OWNER_ONLY_PAYLOAD], role=Role.OWNER)
    request = StartRequest(session_id=SESSION, text="全厂工资统计")
    record = await service.start(credential(), request)

    events = await drain(service, record.interaction_id)

    assert len(runner.requests) == 1
    assert events[-1].name == "interaction.completed"
    assert store.interactions[str(record.interaction_id)].error_category is None


@pytest.mark.asyncio
async def test_employee_is_denied_an_owner_capability_with_friendly_scope() -> None:
    """An employee asking for a factory-wide capability gets a friendly denial
    naming their actual data range, with zero runner calls."""
    service, store, runner = build([OWNER_ONLY_PAYLOAD], role=Role.EMPLOYEE)
    request = StartRequest(session_id=SESSION, text="全厂工资统计")
    record = await service.start(credential(), request)

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.error_category == "forbidden"
    denial = next(message for message in store.messages if message.kind.value == "error")
    assert "您可查询的范围" in denial.text


@pytest.mark.asyncio
async def test_over_one_year_time_range_is_friendly_rejected_with_zero_calls() -> None:
    """超近一年时间范围被友好终止，不进 MES 调用（客户确认 2）."""
    service, store, runner = build([TOO_WIDE_PAYLOAD], role=Role.EMPLOYEE)
    request = StartRequest(session_id=SESSION, text="两年前的产量")
    record = await service.start(credential(), request)

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.error_category == "time_range_exceeds_limit"
    denial = next(message for message in store.messages if message.kind.value == "error")
    assert "时间范围超出上限（近一年）" in denial.text


@pytest.mark.asyncio
async def test_incomplete_intent_asks_instead_of_executing() -> None:
    service, store, runner = build([INCOMPLETE_PAYLOAD])
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="看看情况"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert any(event.name == INTERACTION_CLARIFICATION for event in events)
    assert store.interactions[str(record.interaction_id)].clarification_rounds == 1


@pytest.mark.asyncio
async def test_clarification_budget_ends_in_a_structured_failure() -> None:
    limits = SessionLimits(max_clarification_rounds=1)
    service, store, runner = build([INCOMPLETE_PAYLOAD], limits=limits)
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="看看情况"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.error_category == "clarification_exhausted"


@pytest.mark.asyncio
async def test_gateway_failure_is_distinguished_from_a_semantic_failure() -> None:
    service, store, _ = build(
        failures=[ModelGatewayError(ModelErrorCategory.TIMEOUT, "gateway request timed out")]
    )
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    await drain(service, record.interaction_id)

    assert store.interactions[str(record.interaction_id)].error_category == "gateway_timeout"


@pytest.mark.asyncio
async def test_unusable_model_output_is_a_semantic_failure() -> None:
    service, store, _ = build(["not json", "still not json"])
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    await drain(service, record.interaction_id)

    assert store.interactions[str(record.interaction_id)].error_category == "model_output_invalid"


@pytest.mark.asyncio
async def test_execution_failure_never_leaks_upstream_detail() -> None:
    runner = RecordingCapabilityRunner(failure=RuntimeError(f"upstream said: {CANARY}"))
    service, store, _ = build(runner=runner)
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    stored = store.interactions[str(record.interaction_id)]
    assert stored.error_category == "execution_failed"
    assert all("E-CANARY" not in str(event.data) for event in events)


@pytest.mark.asyncio
async def test_resume_replays_persisted_events_without_repeating_the_fetch() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    first = await drain(service, record.interaction_id)

    resumed = await drain(service, record.interaction_id, after_sequence=first[1].sequence)

    assert len(runner.requests) == 1
    assert [event.sequence for event in resumed] == [event.sequence for event in first[2:]]


@pytest.mark.asyncio
async def test_resume_from_the_end_yields_nothing_new() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    first = await drain(service, record.interaction_id)

    resumed = await drain(service, record.interaction_id, after_sequence=first[-1].sequence)

    assert resumed == []
    assert len(runner.requests) == 1


@pytest.mark.asyncio
async def test_a_terminal_interaction_is_never_executed_twice() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    await drain(service, record.interaction_id)

    await drain(service, record.interaction_id)
    await drain(service, record.interaction_id)

    assert len(runner.requests) == 1


@pytest.mark.asyncio
async def test_cancel_persists_a_cancelled_terminal_state() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    cancelled = await service.cancel(credential(), record.interaction_id)

    assert cancelled.status is InteractionStatus.CANCELLED
    assert cancelled.state is SessionState.CANCELLED
    assert runner.requests == []


@pytest.mark.asyncio
async def test_cancelled_interaction_stops_before_any_business_call() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    await service.cancel(credential(), record.interaction_id)

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert [event.name for event in events] == ["interaction.cancelled"]


@pytest.mark.asyncio
async def test_cancel_is_idempotent() -> None:
    service, _, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    first = await service.cancel(credential(), record.interaction_id)
    second = await service.cancel(credential(), record.interaction_id)

    assert first.status is second.status is InteractionStatus.CANCELLED


@pytest.mark.asyncio
async def test_another_users_interaction_is_indistinguishable_from_missing() -> None:
    service, store, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    store.interactions[str(record.interaction_id)] = replace(
        store.interactions[str(record.interaction_id)], user_id=UserId("user-b")
    )

    with pytest.raises(InteractionNotFoundError):
        await service.cancel(credential(), record.interaction_id)


@pytest.mark.asyncio
async def test_a_missing_interaction_raises_the_same_error() -> None:
    service, _, _ = build()

    with pytest.raises(InteractionNotFoundError):
        await service.cancel(credential(), InteractionId("does-not-exist"))


@pytest.mark.asyncio
async def test_unknown_credentials_are_rejected_before_persistence() -> None:
    service, store, runner = build()

    with pytest.raises(IdentityRejectionError):
        await service.start(
            TrustedCredential(tenant_id=TenantId("tenant-x"), user_id=UserId("user-x")),
            StartRequest(session_id=SESSION, text="上个月产量"),
        )

    assert store.interactions == {}
    assert runner.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "x" * 5000])
async def test_unacceptable_input_is_refused(text: str) -> None:
    service, store, _ = build()

    with pytest.raises(ValueError):
        await service.start(credential(), StartRequest(session_id=SESSION, text=text))

    assert store.interactions == {}


@pytest.mark.asyncio
async def test_usage_events_are_written_in_the_same_commit_and_stay_pseudonymous() -> None:
    service, store, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text=CANARY[:50]))

    await drain(service, record.interaction_id)

    kinds = {event.event_type for event in store.usage_events}
    assert kinds == {"interaction_started", "llm_call_completed", "interaction_completed"}
    for event in store.usage_events:
        assert event.payload["user_subject_id"] == pseudonymous_subject(
            TenantId("tenant-a"), UserId("user-a")
        )
        assert "E-CANARY" not in str(event.payload)
        assert "prompt" not in event.payload


@pytest.mark.asyncio
async def test_ownership_filter_is_used_for_every_store_read() -> None:
    service, store, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    await drain(service, record.interaction_id)

    foreign = InteractionOwner(tenant_id=TenantId("tenant-b"), user_id=UserId("user-b"))

    assert await store.get_interaction(foreign, record.interaction_id) is None
    assert await store.list_events(foreign, record.interaction_id, 0) == ()


# ---------------------------------------------------------------------------
# Business filter resolution (FR-012 target employee, dept names).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fr012_resolves_target_employee_into_narrowed_filters() -> None:
    """FR-012 resolves the employee via the MES-filtered directory before any
    wage call; the runner receives employee_ids={target} (mes_filtered trust)."""
    service, _, runner = build([ANY_EMPLOYEE_PAYLOAD], role=Role.OWNER)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="查模拟员工甲的工资")
    )

    events = await drain(service, record.interaction_id)

    assert any(event.name == INTERACTION_RESULT for event in events)
    filters = runner.requests[0].filters
    assert filters.employee_ids == frozenset({EmployeeId("emp-1")})
    assert filters.tenant_id == TenantId("tenant-a")


@pytest.mark.asyncio
async def test_fr012_ambiguous_name_asks_for_uid_not_run() -> None:
    """同名员工追问稳定 uid，不用姓名关联（FR-012）。"""
    directory = FakeDirectory(
        employee_error=DirectoryError("ambiguous", "员工「模拟员工甲」存在同名，请提供工号")
    )
    service, _, runner = build([ANY_EMPLOYEE_PAYLOAD], directory=directory, role=Role.OWNER)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="查模拟员工甲的工资")
    )

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert any(event.name == INTERACTION_CLARIFICATION for event in events)
    clarifying = next(e for e in events if e.name == INTERACTION_CLARIFICATION)
    assert "工号" in str(clarifying.data["question"])


@pytest.mark.asyncio
async def test_fr012_unresolved_employee_rejects_with_zero_business_calls() -> None:
    """解析不到目标员工 → 直接拒绝且零业务调用（FR-012）。"""
    directory = FakeDirectory(
        employee_error=DirectoryError("not_found", "未找到员工「不存在的人」")
    )
    service, store, runner = build([ANY_EMPLOYEE_PAYLOAD], directory=directory, role=Role.OWNER)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="查不存在的人的工资")
    )

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    assert store.interactions[str(record.interaction_id)].error_category == "filter_not_found"
