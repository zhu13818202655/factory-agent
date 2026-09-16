from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace

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
from factory_agent.application.chitchat import ChatResponder
from factory_agent.application.context import ConversationTurn
from factory_agent.application.intent import CapabilityCatalog, CapabilitySpec
from factory_agent.application.session import (
    DrillPayload,
    InteractionNotFoundError,
    SessionLimits,
    SessionService,
    StartRequest,
)
from factory_agent.application.usage import pseudonymous_subject
from factory_agent.domain import (
    INTERACTION_ANSWER,
    INTERACTION_CLARIFICATION,
    INTERACTION_PHASE,
    INTERACTION_PROGRESS,
    INTERACTION_RESULT,
    INTERACTION_STARTED,
    CapabilityId,
    DataScope,
    DeptId,
    EmployeeId,
    InteractionId,
    InteractionStatus,
    MessageKind,
    MessageRole,
    Role,
    SessionEvent,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.observability.context import bind_interaction_id, current_log_context
from factory_agent.ports import (
    InteractionCommit,
    InteractionOwner,
    ModelErrorCategory,
    ModelGatewayError,
)
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
        CapabilitySpec(
            capability_id=CapabilityId("chitchat"),
            title="闲聊与常识问答",
            description="处理问候、寒暄以及与工厂业务无关的常识问答。",
            required_slots=(),
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
CHITCHAT_PAYLOAD = '{"capability_id": "chitchat", "confidence": 0.95, "slots": {}}'
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
        return (EmployeeRecord("emp-1", "模拟员工甲", "MNYGJ", dept="dept-1"),)


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


@dataclass
class _StateRecordingStore(InMemoryInteractionStore):
    """In-memory store that also records the state each commit persisted."""

    persisted_states: list[SessionState] = field(default_factory=lambda: [])

    async def commit(self, commit: InteractionCommit) -> None:
        self.persisted_states.append(commit.interaction.state)
        await super().commit(commit)


def build(
    contents: list[str] | None = None,
    *,
    role: Role = Role.EMPLOYEE,
    failures: list[Exception | None] | None = None,
    runner: RecordingCapabilityRunner | None = None,
    limits: SessionLimits | None = None,
    store: InMemoryInteractionStore | None = None,
    directory: FakeDirectory | None = None,
    chat_text: str | None = None,
    chat_failure: Exception | None = None,
    scope_verdict: str | None = None,
    scope_failure: Exception | None = None,
) -> tuple[SessionService, InMemoryInteractionStore, RecordingCapabilityRunner]:
    from factory_agent.application.intent import CapabilityIntentParser

    gateway = ScriptedModelGateway(contents=contents or [INTENT_PAYLOAD], failures=failures or [])
    parser = CapabilityIntentParser(
        gateway, CATALOG, model_alias="factory-fast", timezone_name="Asia/Shanghai"
    )
    chat = None
    if chat_text is not None or chat_failure is not None:
        chat = ChatResponder(
            ScriptedModelGateway(
                contents=[chat_text] if chat_text is not None else [],
                failures=[chat_failure] if chat_failure is not None else [],
            ),
            model_alias="factory-summary",
        )
    guard = None
    if scope_verdict is not None or scope_failure is not None:
        from factory_agent.application.scope_guard import ScopeGuard

        guard = ScopeGuard(
            ScriptedModelGateway(
                contents=[scope_verdict] if scope_verdict is not None else [],
                failures=[scope_failure] if scope_failure is not None else [],
            ),
            model_alias="factory-summary",
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
        chat=chat,
        scope_guard=guard,
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
async def test_start_binds_the_interaction_to_the_log_correlation_context() -> None:
    bind_interaction_id(None)
    service, _, _ = build()

    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    assert current_log_context()["interaction_id"] == str(record.interaction_id)


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
async def test_progress_events_announce_every_silent_stage_before_it_runs() -> None:
    """The stages that produce no other event are announced before they start."""
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    progress = [event for event in events if event.name == INTERACTION_PROGRESS]
    assert [event.data["stage"] for event in progress] == ["解析中", "权限检查中"]
    assert [event.data["reason"] for event in progress] == [
        "parse_started",
        "authorize_started",
    ]
    assert {event.data["status"] for event in progress} == {"running"}
    # A progress event never claims a state the interaction has not reached:
    # the whole authorization chain runs while the record still says PARSING.
    assert {event.data["state"] for event in progress} == {SessionState.PARSING.value}
    # No slot names a department or employee here, so the directory lookup never
    # runs and must not be announced.
    assert "scope_resolution_started" not in [event.data["reason"] for event in progress]
    assert len(runner.requests) == 1


@pytest.mark.asyncio
async def test_named_slots_still_announce_the_directory_lookup() -> None:
    """A slot naming a department or employee reads the MES-filtered directory,
    so that stage is announced before the first lookup."""
    service, _, _ = build([ANY_EMPLOYEE_PAYLOAD], role=Role.OWNER)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="查模拟员工甲的工资")
    )

    events = await drain(service, record.interaction_id)

    progress = [event for event in events if event.name == INTERACTION_PROGRESS]
    assert [event.data["reason"] for event in progress] == [
        "parse_started",
        "authorize_started",
        "scope_resolution_started",
    ]
    assert progress[-1].data["stage"] == "核对数据范围"


@pytest.mark.asyncio
async def test_adjacent_phase_transitions_never_persist_a_half_advanced_run() -> None:
    """AUTHORIZING and EXECUTING are announced in one commit, so AUTHORIZING is
    never a durable state — while both events keep their own sequence."""
    store = _StateRecordingStore()
    service, _, _ = build(store=store)
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    assert SessionState.AUTHORIZING not in store.persisted_states
    phases = [event for event in events if event.name == INTERACTION_PHASE]
    assert [event.data["reason"] for event in phases] == [
        "intent_complete",
        "authorized",
        "execution_complete",
    ]
    assert [event.sequence for event in phases] == [
        phases[0].sequence,
        phases[0].sequence + 1,
        phases[0].sequence + 2,
    ]
    assert store.interactions[str(record.interaction_id)].state is SessionState.ANSWERED


@pytest.mark.asyncio
async def test_progress_events_come_before_the_phase_events_they_announce() -> None:
    service, _, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    assert (events[0].name, events[1].name) == (INTERACTION_STARTED, INTERACTION_PROGRESS)
    first_phase = next(
        index for index, event in enumerate(events) if event.name == INTERACTION_PHASE
    )
    last_progress = max(
        index for index, event in enumerate(events) if event.name == INTERACTION_PROGRESS
    )
    assert last_progress < first_phase


@pytest.mark.asyncio
async def test_progress_events_are_persisted_for_replay() -> None:
    service, store, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    await drain(service, record.interaction_id)

    stored = [event.name for event in store.events[str(record.interaction_id)]]
    # Two: 解析中 and 权限检查中. The directory-lookup announcement is skipped
    # because no slot names a department or employee.
    assert stored.count(INTERACTION_PROGRESS) == 2


@pytest.mark.asyncio
async def test_progress_events_replay_identically_on_reconnect() -> None:
    service, _, runner = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    live = await drain(service, record.interaction_id)

    replayed = await drain(service, record.interaction_id)

    assert replayed == live
    assert len(runner.requests) == 1


@pytest.mark.asyncio
async def test_chitchat_only_announces_the_parse_stage() -> None:
    """No business call runs, so the authorization windows are never announced."""
    service, _, _ = build([CHITCHAT_PAYLOAD], chat_text="你好呀！")
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    events = await drain(service, record.interaction_id)

    progress = [event.data["stage"] for event in events if event.name == INTERACTION_PROGRESS]
    assert progress == ["解析中"]


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
async def test_successful_run_logs_the_final_outcome_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The final result sent to the front end is logged (metadata only)."""
    service, _, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    caplog.set_level(0)
    await drain(service, record.interaction_id)

    assert "session.outcome.result" in caplog.text
    assert "rows=" in caplog.text


@pytest.mark.asyncio
async def test_result_commit_persists_distinct_message_sequences() -> None:
    """Result-table and answer messages each anchor a distinct event sequence.

    The ``agent_message_sequence_key`` unique constraint rejects two messages
    sharing one (interaction, sequence) pair; the in-memory store now mirrors
    that, so this also guards the streaming outcome (a failed commit means no
    result/terminal event ever reaches the front end).
    """
    service, store, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    result_sequence = next(event.sequence for event in events if event.name == INTERACTION_RESULT)
    committed = [
        message for message in store.messages if message.interaction_id == record.interaction_id
    ]
    assistant_messages = [m for m in committed if m.role is MessageRole.ASSISTANT]
    kinds = [message.kind for message in assistant_messages]
    assert kinds.count(MessageKind.RESULT_TABLE) == 1
    assert kinds.count(MessageKind.PLAIN_TEXT) == 1
    sequences = [message.sequence for message in committed]
    assert len(sequences) == len(set(sequences))
    answer_message = next(m for m in assistant_messages if m.kind is MessageKind.PLAIN_TEXT)
    assert answer_message.text
    table_message = next(m for m in assistant_messages if m.kind is MessageKind.RESULT_TABLE)
    assert table_message.sequence == result_sequence


@pytest.mark.asyncio
async def test_result_event_carries_column_titles() -> None:
    """The result event exposes Chinese display labels alongside raw names."""
    service, _, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    result_event = next(event for event in events if event.name == INTERACTION_RESULT)
    titles = result_event.data["column_titles"]
    assert isinstance(titles, list)
    assert len(titles) == len(result_event.data["columns"])


@pytest.mark.asyncio
async def test_result_event_and_message_carry_the_same_card_payload() -> None:
    """The card dict is identical on the SSE event and the persisted message."""
    card = {
        "kind": "kpi",
        "title": "个人工资汇总",
        "metrics": [{"label": "计件工资合计", "unit": "元", "value": "8650"}],
    }
    service, store, runner = build()
    runner.card = card
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    result_event = next(event for event in events if event.name == INTERACTION_RESULT)
    assert result_event.data["card"] == card
    table_message = next(
        m
        for m in store.messages
        if m.interaction_id == record.interaction_id and m.kind is MessageKind.RESULT_TABLE
    )
    assert table_message.payload["card"] == card


@pytest.mark.asyncio
async def test_result_without_card_keeps_the_old_contract() -> None:
    service, store, _ = build()
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))

    events = await drain(service, record.interaction_id)

    result_event = next(event for event in events if event.name == INTERACTION_RESULT)
    assert "card" not in result_event.data
    table_message = next(
        m
        for m in store.messages
        if m.interaction_id == record.interaction_id and m.kind is MessageKind.RESULT_TABLE
    )
    assert "card" not in table_message.payload


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
async def test_fr012_group_leader_member_payroll_is_authorized_locally() -> None:
    """D-5（2026-09-16 拍板）：组长查组内成员工资明细本地链路全程放行.

    403 只可能来自客户 MES（code=-403 业务级拒绝），本地矩阵 01/02/99 均允许
    FR-012；该测试钉住本地不变量：组长点名本部门员工 → 目标员工进入
    NarrowedFilters、业务调用照常发出、终态是 completed 而非 rejected。
    """
    service, store, runner = build([ANY_EMPLOYEE_PAYLOAD], role=Role.MANAGER)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="查模拟员工甲的工资")
    )

    events = await drain(service, record.interaction_id)

    assert any(event.name == INTERACTION_RESULT for event in events)
    assert events[-1].name == "interaction.completed"
    failed = [e for e in events if e.name == "interaction.failed"]
    assert failed == []
    filters = runner.requests[0].filters
    assert filters.employee_ids == frozenset({EmployeeId("emp-1")})
    assert filters.tenant_id == TenantId("tenant-a")
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED


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


# ---------------------------------------------------------------------------
# Chit-chat turns (reserved capability, zero business calls).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chitchat_answers_free_form_text_with_zero_business_calls() -> None:
    reply = "你好呀！我是工厂助手，今天想聊点什么？"
    service, store, runner = build([CHITCHAT_PAYLOAD], chat_text=reply)
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    answer = next(event for event in events if event.name == INTERACTION_ANSWER)
    assert answer.data["text"] == reply
    assert events[-1].name == "interaction.completed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.state is SessionState.ANSWERED
    assert stored.status is InteractionStatus.COMPLETED
    assert stored.clarification_rounds == 0
    assert stored.capability_id is None
    assert [m.text for m in store.messages if m.kind is MessageKind.CHAT] == [reply]


@pytest.mark.asyncio
async def test_chitchat_records_only_extract_and_chat_llm_usage_events() -> None:
    service, store, runner = build([CHITCHAT_PAYLOAD], chat_text="你好呀！")
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    await drain(service, record.interaction_id)

    assert runner.requests == []
    llm = [
        event.payload
        for event in store.usage_events
        if event.payload["event_type"] == "llm_call_completed"
    ]
    assert [event["stage"] for event in llm] == ["extract", "chat"]
    assert llm[1]["model_alias"] == "factory-summary"


@pytest.mark.asyncio
async def test_chitchat_logs_the_answer_text(caplog: pytest.LogCaptureFixture) -> None:
    """The chat answer returned to the front end is logged."""
    service, _, _ = build([CHITCHAT_PAYLOAD], chat_text="你好呀！")
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    caplog.set_level(0)
    await drain(service, record.interaction_id)

    assert "session.outcome.chat" in caplog.text
    assert "你好呀！" in caplog.text


@pytest.mark.asyncio
async def test_chitchat_without_a_responder_falls_back_safely() -> None:
    service, store, runner = build([CHITCHAT_PAYLOAD])
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.error_category == "capability_unresolved"


@pytest.mark.asyncio
async def test_chitchat_gateway_failure_is_a_friendly_failure() -> None:
    service, store, runner = build(
        [CHITCHAT_PAYLOAD],
        chat_failure=ModelGatewayError(ModelErrorCategory.TIMEOUT, "chat upstream timed out"),
    )
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.error_category == "gateway_timeout"


@pytest.mark.asyncio
async def test_low_confidence_chitchat_is_clarified_not_answered() -> None:
    payload = '{"capability_id": "chitchat", "confidence": 0.2, "slots": {}}'
    service, store, runner = build([payload], chat_text="被忽略的回复")
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="随便聊聊"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert not any(event.name == INTERACTION_ANSWER for event in events)
    assert any(event.name == INTERACTION_CLARIFICATION for event in events)
    assert store.interactions[str(record.interaction_id)].clarification_rounds == 1


# ---------------------------------------------------------------------------
# Merged chit-chat (single intent call) and multi-turn rewrite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chitchat_merged_content_answers_without_a_second_model_call() -> None:
    """type=chitchat + content answers directly; zero CHAT stage calls."""
    payload = (
        '{"type": "chitchat", "capability_id": "chitchat", "confidence": 0.95, '
        '"slots": {}, "content": "你好呀！我是工厂助手，今天想聊点什么？"}'
    )
    # chat is intentionally NOT configured: the merged content must answer.
    service, store, runner = build([payload])
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    answer = next(event for event in events if event.name == INTERACTION_ANSWER)
    assert answer.data["text"] == "你好呀！我是工厂助手，今天想聊点什么？"
    assert events[-1].name == "interaction.completed"
    stored = store.interactions[str(record.interaction_id)]
    assert stored.status is InteractionStatus.COMPLETED
    llm = [
        event.payload
        for event in store.usage_events
        if event.payload["event_type"] == "llm_call_completed"
    ]
    assert [event["stage"] for event in llm] == ["extract"]


@pytest.mark.asyncio
async def test_chitchat_without_content_still_uses_the_responder_fallback() -> None:
    """Backward-compatible path: legacy/empty payloads keep the CHAT call."""
    reply = "你好呀！我是工厂助手。"
    service, store, _ = build([CHITCHAT_PAYLOAD], chat_text=reply)
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="你好"))

    await drain(service, record.interaction_id)

    answer = next(event for event in store.messages if event.kind is MessageKind.CHAT)
    assert answer.text == reply
    llm = [
        event.payload
        for event in store.usage_events
        if event.payload["event_type"] == "llm_call_completed"
    ]
    assert [event["stage"] for event in llm] == ["extract", "chat"]


@pytest.mark.asyncio
async def test_follow_up_auto_builds_history_from_the_same_session() -> None:
    """The second interaction in a session sees the first turn as context.

    Regression guard for the multi-turn root cause: the API never passed an
    explicit history, so the pipeline must rebuild it from persisted rows.
    """
    from factory_agent.application.intent import CapabilityIntentParser

    store = InMemoryInteractionStore()
    gateway = ScriptedModelGateway(contents=[INTENT_PAYLOAD, INTENT_PAYLOAD])
    service = SessionService(
        store,
        authorization(),
        CapabilityIntentParser(
            gateway, CATALOG, model_alias="factory-fast", timezone_name="Asia/Shanghai"
        ),
        RecordingCapabilityRunner(),
        FrozenClock(NOW),
        new_id=SequentialIds(),
        sleep=_no_sleep,
        business_filters=BusinessFilterResolver(FakeDirectory()),
    )
    first = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    await drain(service, first.interaction_id)
    second = await service.start(
        credential(), StartRequest(session_id=SESSION, text="那这个月呢？")
    )
    await drain(service, second.interaction_id)

    assert len(gateway.requests) == 2
    second_prompt = " ".join(message.content for message in gateway.requests[1].messages)
    assert "上个月产量" in second_prompt
    assert "FR-001" in second_prompt


@pytest.mark.asyncio
async def test_rewritten_follow_up_is_echoed_back_on_clarification() -> None:
    """A rewritten query is surfaced when the follow-up still needs detail."""
    payload = (
        '{"type": "capability", "capability_id": null, "confidence": 0.9, '
        '"slots": {}, "rewrite_query": "查询我这个月的工资明细"}'
    )
    service, store, _ = build([payload])
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="那这个月呢？")
    )

    events = await drain(service, record.interaction_id)

    clarification = next(event for event in events if event.name == INTERACTION_CLARIFICATION)
    question = clarification.data["question"]
    assert "查询我这个月的工资明细" in question
    assert store.interactions[str(record.interaction_id)].clarification_rounds == 1


SCOPE_BEYOND_PAYLOAD = '{"verdict": "beyond", "target": "全组的工资明细"}'
SCOPE_WITHIN_PAYLOAD = '{"verdict": "within", "target": ""}'


@pytest.mark.asyncio
async def test_scope_guard_denies_out_of_range_request_before_any_business_call() -> None:
    """An employee asking for the whole group's wage detail is denied with a
    friendly message before any capability run or MES call."""
    from factory_agent.domain import INTERACTION_FAILED

    service, store, runner = build(scope_verdict=SCOPE_BEYOND_PAYLOAD)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="我想知道全组的工资明细")
    )

    events = await drain(service, record.interaction_id)

    failed = [event for event in events if event.name == INTERACTION_FAILED]
    assert len(failed) == 1
    assert failed[0].data["error_category"] == "scope_forbidden"
    assert "全组的工资明细" in failed[0].data["message"]
    assert "本人" in failed[0].data["message"]
    # No business-data call ever happened.
    assert runner.requests == []
    stored = store.interactions[str(record.interaction_id)]
    assert stored.status is InteractionStatus.FAILED
    assert stored.error_category == "scope_forbidden"
    # The denial text is persisted so history replay shows it.
    kinds = [(message.kind, message.text) for message in store.messages]
    assert any(kind is MessageKind.ERROR and "全组的工资明细" in text for kind, text in kinds)


@pytest.mark.asyncio
async def test_scope_guard_within_range_proceeds_to_execution() -> None:
    service, store, runner = build(scope_verdict=SCOPE_WITHIN_PAYLOAD)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="我上个月的工资明细")
    )

    events = await drain(service, record.interaction_id)

    assert events[-1].name == "interaction.completed"
    assert len(runner.requests) == 1
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED


@pytest.mark.asyncio
async def test_scope_guard_failure_fails_open_without_blocking_the_run() -> None:
    """A guard model failure never blocks the run: MES row filtering still
    bounds every returned row, so the interaction proceeds normally."""
    service, store, runner = build(
        scope_failure=ModelGatewayError(
            ModelErrorCategory.UNAVAILABLE, "gateway unreachable", duration_ms=5
        )
    )
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="我上个月的工资明细")
    )

    events = await drain(service, record.interaction_id)

    assert events[-1].name == "interaction.completed"
    assert len(runner.requests) == 1
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Structured drill rounds (D-3/D-5 拍板，2026-09-16).
# ---------------------------------------------------------------------------


def _drill_runner(capability_ids: frozenset[str]) -> RecordingCapabilityRunner:
    runner = RecordingCapabilityRunner()
    runner.recipes = SimpleNamespace(capability_ids=capability_ids)
    return runner


@pytest.mark.asyncio
async def test_drill_round_runs_the_capability_without_a_model_call() -> None:
    """结构化下钻：意图来自 drill 载荷，跳过 LLM 路由，槽位直连 FilterNarrower."""
    runner = _drill_runner(capability_ids=frozenset({"fr008_payroll_ranking"}))
    service, store, _ = build(role=Role.MANAGER, runner=runner)
    record = await service.start(
        credential(),
        StartRequest(
            session_id=SESSION,
            text="查看该车间工资",
            drill=DrillPayload(
                capability_id="fr008_payroll_ranking",
                dept_ids=("dept-1",),
            ),
        ),
    )

    events = await drain(service, record.interaction_id)

    assert any(event.name == INTERACTION_RESULT for event in events)
    request = runner.requests[0]
    assert request.capability_id == CapabilityId("fr008_payroll_ranking")
    assert request.filters.dept_ids == frozenset({DeptId("dept-1")})
    # 零真实模型调用：EXTRACT 计量事件标记为 structured_drill，次数为 0。
    extract = [
        event
        for event in store.usage_events
        if event.payload.get("stage") == "extract"
        or event.payload.get("actual_model") == "structured_drill"
    ]
    assert extract, "expected the structured-drill EXTRACT metering event"
    assert extract[0].payload.get("actual_model") == "structured_drill"


@pytest.mark.asyncio
async def test_drill_rejects_an_unregistered_capability_at_the_boundary() -> None:
    """drill 指向未登记能力 → start 阶段 400（不落交互、零下游调用）。"""
    runner = _drill_runner(capability_ids=frozenset({"fr008_payroll_ranking"}))
    service, store, _ = build(role=Role.MANAGER, runner=runner)

    with pytest.raises(ValueError):
        await service.start(
            credential(),
            StartRequest(
                session_id=SESSION,
                text="查看该车间工资",
                drill=DrillPayload(capability_id="fr999_unknown", dept_ids=("dept-1",)),
            ),
        )
    assert store.interactions == {}


@pytest.mark.asyncio
async def test_drill_out_of_range_dept_is_rejected_before_any_business_call() -> None:
    """drill 部门越界（不在调用者绑定范围内）→ 拒绝且零业务调用."""
    runner = _drill_runner(capability_ids=frozenset({"fr008_payroll_ranking"}))
    service, store, _ = build(role=Role.MANAGER, runner=runner)
    record = await service.start(
        credential(),
        StartRequest(
            session_id=SESSION,
            text="查看其他车间工资",
            drill=DrillPayload(capability_id="fr008_payroll_ranking", dept_ids=("dept-9",)),
        ),
    )

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    assert store.interactions[str(record.interaction_id)].error_category == "filter_forbidden"


@pytest.mark.asyncio
async def test_drill_target_employee_inside_scope_runs_with_narrowed_employee() -> None:
    """D-5：01/02 下钻点成员工资条 —— 目标员工在本部门内 → 放行并收窄到该员工."""
    runner = _drill_runner(capability_ids=frozenset({"fr012_employee_payroll"}))
    directory = FakeDirectory()
    service, _, _ = build(role=Role.MANAGER, runner=runner, directory=directory)
    record = await service.start(
        credential(),
        StartRequest(
            session_id=SESSION,
            text="查看该成员的工资",
            drill=DrillPayload(
                capability_id="fr012_employee_payroll",
                employee_uid="emp-1",
            ),
        ),
    )

    events = await drain(service, record.interaction_id)

    assert any(event.name == INTERACTION_RESULT for event in events)
    request = runner.requests[0]
    assert request.capability_id == CapabilityId("fr012_employee_payroll")
    assert request.filters.employee_ids == frozenset({EmployeeId("emp-1")})


@pytest.mark.asyncio
async def test_drill_target_employee_without_bound_dept_is_rejected() -> None:
    """D-5：目标员工解析不到或不在调用者绑定部门内 → 服务端拒绝、零业务调用.

    FakeDirectory 只返回无部门归属的 emp-1；换成查 emp-9 时目录查不到，两条
    路径都必须在业务调用前拒绝（01/02 不能点本部门之外的成员）。
    """
    runner = _drill_runner(capability_ids=frozenset({"fr012_employee_payroll"}))
    service, store, _ = build(role=Role.MANAGER, runner=runner)
    record = await service.start(
        credential(),
        StartRequest(
            session_id=SESSION,
            text="查看该成员的工资",
            drill=DrillPayload(capability_id="fr012_employee_payroll", employee_uid="emp-9"),
        ),
    )

    events = await drain(service, record.interaction_id)

    assert runner.requests == []
    assert events[-1].name == "interaction.failed"
    assert store.interactions[str(record.interaction_id)].error_category == "filter_not_found"
