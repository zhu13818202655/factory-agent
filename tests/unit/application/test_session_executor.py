"""Interaction execution decoupled from the SSE connection.

Every test here proves one half of the invariant "the executor's lifecycle is
bound to the interaction, never to a connection": a disconnected claimer, a
crashed pipeline, a cancel, and the wall-clock budget all end in a durable
terminal state, while the business call happens at most once.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.business_filters import (
    BusinessFilterResolver,
    DeptRecord,
    EmployeeRecord,
)
from factory_agent.application.intent import (
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
)
from factory_agent.application.session import SessionLimits, SessionService
from factory_agent.domain import (
    CapabilityId,
    DataScope,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageKind,
    Role,
    SessionEvent,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.ports import (
    InteractionCommit,
    InteractionOwner,
    ModelRequest,
    ModelResponse,
    ModelUsage,
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
    SequentialIds,
)

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
SESSION = SessionId("session-1")
OWNER = InteractionOwner(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))
INTENT_PAYLOAD = (
    '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
)

CATALOG = CapabilityCatalog(
    specs=(
        CapabilitySpec(
            capability_id=CapabilityId("FR-001"),
            title="查看本人产量",
            required_slots=("time_range",),
        ),
    )
)


class FakeDirectory:
    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]:
        return (DeptRecord("dept-1", "一车间", "YCJ"),)

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]:
        return (EmployeeRecord("emp-1", "模拟员工甲", "MNYGJ"),)


@dataclass
class GatedModelGateway:
    """Model gateway that can block its parse call for concurrency control."""

    contents: list[str] = field(default_factory=lambda: [INTENT_PAYLOAD])
    #: Block the first call until ``gate`` is set.
    wait_for_gate: bool = False
    #: Or simply sleep this long before answering (wall-clock budget tests).
    block_seconds: float = 0.0
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    entered: bool = False
    requests: list[ModelRequest] = field(default_factory=lambda: [])

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.entered = True
        if self.wait_for_gate:
            await self.gate.wait()
        if self.block_seconds:
            await asyncio.sleep(self.block_seconds)
        return ModelResponse(
            content=self.contents[min(len(self.requests) - 1, len(self.contents) - 1)],
            actual_model="qwen3-32b-local",
            usage=ModelUsage(prompt_tokens=11, completion_tokens=5),
            duration_ms=3,
        )


@dataclass
class FlakyCommitStore(InMemoryInteractionStore):
    """In-memory store whose Nth commit raises, simulating a store failure."""

    fail_on: set[int] = field(default_factory=lambda: {2})
    commit_calls: int = 0

    async def commit(self, commit: InteractionCommit) -> None:
        self.commit_calls += 1
        if self.commit_calls in self.fail_on:
            raise ValueError("simulated store failure")
        await super().commit(commit)


def credential() -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))


def authorization() -> AuthorizationService:
    member = membership("user-a", "tenant-a", "emp-1", Role.EMPLOYEE)
    return AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={("tenant-a", "user-a"): member}
        ),
        organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
        versions=FixedScopeVersionAssigner(),
    )


async def _no_sleep(_: float) -> None:
    return None


def build(
    gateway: GatedModelGateway,
    *,
    limits: SessionLimits | None = None,
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
    store: InMemoryInteractionStore | None = None,
) -> tuple[SessionService, InMemoryInteractionStore, RecordingCapabilityRunner]:
    parser = CapabilityIntentParser(
        gateway,  # pyright: ignore[reportArgumentType]
        CATALOG,
        model_alias="factory-fast",
        timezone_name="Asia/Shanghai",
    )
    resolved_store = store or InMemoryInteractionStore()
    runner = RecordingCapabilityRunner()
    service = SessionService(
        resolved_store,
        authorization(),
        parser,
        runner,
        FrozenClock(NOW),
        new_id=SequentialIds(),
        limits=limits,
        sleep=sleep,
        business_filters=BusinessFilterResolver(FakeDirectory()),
    )
    return service, resolved_store, runner


async def seed_pending(service: SessionService) -> InteractionId:
    record = await service.start(credential(), _start_request())
    return record.interaction_id


def _start_request():
    from factory_agent.application.session import StartRequest

    return StartRequest(session_id=SESSION, text="上个月产量")


def completion_events(store: InMemoryInteractionStore) -> list[dict[str, object]]:
    return [
        event.payload
        for event in store.usage_events
        if event.payload.get("event_type") == "interaction_completed"
    ]


@pytest.mark.asyncio
async def test_disconnected_claimer_does_not_kill_the_run() -> None:
    """3.4.1: closing the stream mid-run leaves the executor finishing alone."""
    gateway = GatedModelGateway(wait_for_gate=True)
    limits = SessionLimits(heartbeat_seconds=0.02)
    service, store, runner = build(gateway, limits=limits, sleep=asyncio.sleep)
    interaction_id = await seed_pending(service)

    stream = cast(AsyncGenerator[SessionEvent, None], service.stream(credential(), interaction_id))
    first = await stream.__anext__()
    assert first.name == "interaction.started"
    # Simulate a client disconnect (front-end timeout, proxy cut, browser close)
    # while the pipeline is still executing.
    await stream.aclose()
    assert gateway.entered

    gateway.gate.set()
    await service.shutdown(timeout=2.0)

    stored = store.interactions[str(interaction_id)]
    assert stored.status is InteractionStatus.COMPLETED
    assert stored.state is SessionState.ANSWERED
    names = [event.name for event in store.events[str(interaction_id)]]
    assert names[0] == "interaction.started"
    assert names[-1] == "interaction.completed"
    assert "interaction.result" in names
    assert len(runner.requests) == 1
    assert [event["error_category"] for event in completion_events(store)] == [None]


@pytest.mark.asyncio
async def test_two_connections_share_one_execution_and_one_terminal() -> None:
    """3.4.2: exactly one executor runs; both connections see every event."""
    gateway = GatedModelGateway()
    service, store, runner = build(gateway)
    interaction_id = await seed_pending(service)

    async def drain() -> list[SessionEvent]:
        stream = service.stream(credential(), interaction_id)
        return [event async for event in stream]

    first, second = await asyncio.gather(drain(), drain())

    assert first == second
    assert first[0].name == "interaction.started"
    assert first[-1].name == "interaction.completed"
    assert len(runner.requests) == 1
    stored = store.interactions[str(interaction_id)]
    assert stored.status is InteractionStatus.COMPLETED


@pytest.mark.asyncio
async def test_unexpected_pipeline_crash_persists_a_failed_terminal() -> None:
    """3.4.3: a store failure mid-run still ends in a durable failed terminal."""
    gateway = GatedModelGateway()
    # start() 落库 = #1，started 事件落库 = #2，_pipeline 首个进度事件落库 = #3：
    # 让提交在管线内部炸掉，验证 _run 的兜底守卫。
    store = FlakyCommitStore(fail_on={3})
    service, store, runner = build(gateway, store=store)
    interaction_id = await seed_pending(service)

    events = [event async for event in service.stream(credential(), interaction_id)]

    stored = store.interactions[str(interaction_id)]
    assert stored.status is InteractionStatus.FAILED
    assert stored.error_category == "executor_lost"
    assert events[-1].name == "interaction.failed"
    assert events[-1].data["error_category"] == "executor_lost"
    assert len(runner.requests) == 0  # the crash happened before the business call
    assert [event["error_category"] for event in completion_events(store)] == ["executor_lost"]


@pytest.mark.asyncio
async def test_cancel_stops_the_executor_before_the_business_call() -> None:
    """3.4.4: cancel at a cooperative stop point leaks zero MES calls."""
    gateway = GatedModelGateway(wait_for_gate=True)
    limits = SessionLimits(heartbeat_seconds=0.02, follow_timeout_seconds=2.0)
    service, store, runner = build(gateway, limits=limits, sleep=asyncio.sleep)
    interaction_id = await seed_pending(service)

    drain_task = asyncio.create_task(
        _drain_all(service, interaction_id),
    )
    while not gateway.entered:
        await asyncio.sleep(0)
    cancelled = await service.cancel(credential(), interaction_id)
    gateway.gate.set()

    await drain_task
    await service.shutdown(timeout=2.0)

    assert cancelled.status is InteractionStatus.CANCELLED
    stored = store.interactions[str(interaction_id)]
    assert stored.status is InteractionStatus.CANCELLED
    # The run never reached the business call: the executor stopped at the
    # cooperative stop point right after parse.
    assert runner.requests == []
    assert [event["error_category"] for event in completion_events(store)] == ["cancelled"]


@pytest.mark.asyncio
async def test_cancel_from_another_process_stops_via_the_durable_status() -> None:
    """The DB status is the cross-process cancel channel at stop points."""
    gateway = GatedModelGateway(wait_for_gate=True)
    limits = SessionLimits(heartbeat_seconds=0.02, follow_timeout_seconds=2.0)
    service, store, runner = build(gateway, limits=limits, sleep=asyncio.sleep)
    interaction_id = await seed_pending(service)

    drain_task = asyncio.create_task(_drain_all(service, interaction_id))
    while not gateway.entered:
        await asyncio.sleep(0)
    # Another process wrote the terminal; no in-process event is set.
    store.interactions[str(interaction_id)] = replace(
        store.interactions[str(interaction_id)],
        status=InteractionStatus.CANCELLED,
        state=SessionState.CANCELLED,
    )
    gateway.gate.set()

    await drain_task
    await service.shutdown(timeout=2.0)

    assert runner.requests == []


@pytest.mark.asyncio
async def test_run_budget_expires_and_fails_the_interaction_without_mes_calls() -> None:
    """3.3.4: a run dragging past its wall-clock budget fails with run_timeout."""
    gateway = GatedModelGateway(block_seconds=0.3)
    limits = SessionLimits(
        heartbeat_seconds=0.02,
        follow_timeout_seconds=600.0,
        run_timeout_seconds=0.05,
    )
    service, store, runner = build(gateway, limits=limits, sleep=asyncio.sleep)
    interaction_id = await seed_pending(service)

    events = [event async for event in service.stream(credential(), interaction_id)]

    stored = store.interactions[str(interaction_id)]
    assert stored.status is InteractionStatus.FAILED
    assert stored.error_category == "run_timeout"
    assert events[-1].name == "interaction.failed"
    assert events[-1].data["error_category"] == "run_timeout"
    # The budget fired at the stop point after parse: no business call, no
    # further spend.
    assert runner.requests == []
    assert [event["error_category"] for event in completion_events(store)] == ["run_timeout"]


@pytest.mark.asyncio
async def test_startup_sweep_fails_only_stale_running_and_is_idempotent() -> None:
    store = InMemoryInteractionStore()
    service, store, _runner = build(
        GatedModelGateway(), store=store, limits=SessionLimits(stale_running_seconds=600)
    )
    stale_a = _seed_running(store, "i-stale-a", updated_at=NOW - timedelta(seconds=700))
    stale_b = _seed_running(store, "i-stale-b", updated_at=NOW - timedelta(seconds=700))
    _seed_running(store, "i-fresh", updated_at=NOW - timedelta(seconds=10))
    _seed_running(store, "i-done", updated_at=NOW - timedelta(seconds=700))
    store.interactions["i-done"] = replace(
        store.interactions["i-done"], status=InteractionStatus.COMPLETED
    )

    count = await service.sweep_stale_runs()

    assert count == 2
    assert store.interactions["i-stale-a"].status is InteractionStatus.FAILED
    assert store.interactions["i-stale-a"].error_category == "executor_lost"
    assert store.interactions["i-stale-b"].status is InteractionStatus.FAILED
    assert store.interactions["i-fresh"].status is InteractionStatus.RUNNING
    assert store.interactions["i-done"].status is InteractionStatus.COMPLETED
    for interaction_id in (stale_a, stale_b):
        stored_events = store.events[str(interaction_id)]
        assert stored_events[-1].name == "interaction.failed"
        assert stored_events[-1].data["error_category"] == "executor_lost"
        assert [m.kind for m in store.messages if str(m.interaction_id) == str(interaction_id)]
    assert len([e for e in completion_events(store)]) == 2

    # Idempotent: a second sweep (multi-worker restart) marks nothing.
    assert await service.sweep_stale_runs() == 0


@pytest.mark.asyncio
async def test_abandoned_sweep_reaps_only_unclaimed_pending() -> None:
    """A question persisted without any stream claim is terminated, once."""
    store = InMemoryInteractionStore()
    service, store, _runner = build(
        GatedModelGateway(),
        store=store,
        limits=SessionLimits(abandoned_pending_seconds=600, stale_running_seconds=600),
    )
    # A real start(): the question is persisted with its own message at
    # sequence 1 and no connection ever claimed the run.
    abandoned_id = await seed_pending(service)
    store.interactions[str(abandoned_id)] = replace(
        store.interactions[str(abandoned_id)], created_at=NOW - timedelta(seconds=700)
    )
    fresh_id = await seed_pending(service)
    _seed_running(store, "i-running", updated_at=NOW - timedelta(seconds=700))

    count = await service.sweep_abandoned_runs()

    assert count == 1
    reaped = store.interactions[str(abandoned_id)]
    assert reaped.status is InteractionStatus.FAILED
    assert reaped.state is SessionState.FAILED
    assert reaped.error_category == "abandoned"
    assert reaped.completed_at == NOW
    # The reserved terminal event sequence.
    assert reaped.last_event_sequence == 1
    assert store.interactions[str(fresh_id)].status is InteractionStatus.PENDING
    assert store.interactions["i-running"].status is InteractionStatus.RUNNING

    stored_events = store.events[str(abandoned_id)]
    assert [event.name for event in stored_events] == ["interaction.failed"]
    assert stored_events[-1].data["error_category"] == "abandoned"
    assert stored_events[-1].data["message"] == "该提问未能开始执行，请重新发送。"
    # The question already owns message sequence 1, so the notice must take the
    # next slot: agent_message is unique on (interaction_id, sequence).
    messages = [m for m in store.messages if str(m.interaction_id) == str(abandoned_id)]
    assert [(m.kind, m.sequence) for m in messages] == [
        (MessageKind.PLAIN_TEXT, 1),
        (MessageKind.ERROR, 2),
    ]
    assert [event["error_category"] for event in completion_events(store)] == ["abandoned"]

    # Idempotent: a second sweep (multi-worker restart) marks nothing.
    assert await service.sweep_abandoned_runs() == 0


@pytest.mark.asyncio
async def test_reaped_abandoned_question_streams_a_clean_terminal() -> None:
    """A late subscriber replays the terminal and never re-runs the question."""
    store = InMemoryInteractionStore()
    service, store, runner = build(GatedModelGateway(), store=store)
    interaction_id = await seed_pending(service)
    store.interactions[str(interaction_id)] = replace(
        store.interactions[str(interaction_id)], created_at=NOW - timedelta(seconds=700)
    )
    await service.sweep_abandoned_runs()
    commits_before = store.commits

    events = await _drain_all(service, interaction_id)

    assert [event.name for event in events] == ["interaction.failed"]
    assert events[0].data["error_category"] == "abandoned"
    # The reaped row is already terminal, so the connection is a pure subscriber:
    # no claim, no executor, no business call, no further write.
    assert runner.requests == []
    assert store.commits == commits_before


@pytest.mark.asyncio
async def test_periodic_sweep_reaps_both_recovery_classes() -> None:
    """The periodic pass covers abandoned questions and orphaned runs alike."""
    store = InMemoryInteractionStore()
    service, store, _runner = build(
        GatedModelGateway(),
        store=store,
        limits=SessionLimits(abandoned_pending_seconds=600, stale_running_seconds=600),
    )
    abandoned_id = await seed_pending(service)
    store.interactions[str(abandoned_id)] = replace(
        store.interactions[str(abandoned_id)], created_at=NOW - timedelta(seconds=700)
    )
    _seed_running(store, "i-run", updated_at=NOW - timedelta(seconds=700))

    task = asyncio.create_task(service.sweep_forever(0.01))
    try:
        for _ in range(400):
            abandoned_done = (
                store.interactions[str(abandoned_id)].status is InteractionStatus.FAILED
            )
            orphan_done = store.interactions["i-run"].status is InteractionStatus.FAILED
            if abandoned_done and orphan_done:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert store.interactions[str(abandoned_id)].error_category == "abandoned"
    assert store.interactions["i-run"].error_category == "executor_lost"


@pytest.mark.asyncio
async def test_periodic_sweep_survives_a_failing_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery must not become a second outage: one bad pass is skipped."""
    store = InMemoryInteractionStore()
    service, store, _runner = build(
        GatedModelGateway(), store=store, limits=SessionLimits(stale_running_seconds=600)
    )
    _seed_running(store, "i-run", updated_at=NOW - timedelta(seconds=700))
    original = store.fail_abandoned_runs
    calls = {"count": 0}

    async def flaky(
        *, abandoned_before: datetime, now: datetime, category: str
    ) -> tuple[InteractionRecord, ...]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("session store unavailable")
        return await original(abandoned_before=abandoned_before, now=now, category=category)

    monkeypatch.setattr(store, "fail_abandoned_runs", flaky)

    task = asyncio.create_task(service.sweep_forever(0.01))
    try:
        for _ in range(400):
            if store.interactions["i-run"].status is InteractionStatus.FAILED:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert calls["count"] > 1
    assert store.interactions["i-run"].status is InteractionStatus.FAILED


async def _drain_all(service: SessionService, interaction_id: InteractionId) -> list[SessionEvent]:
    stream = cast(AsyncGenerator[SessionEvent, None], service.stream(credential(), interaction_id))
    return [event async for event in stream]


def _seed_running(
    store: InMemoryInteractionStore, interaction_id: str, *, updated_at: datetime
) -> InteractionId:
    store.interactions[interaction_id] = InteractionRecord(
        interaction_id=InteractionId(interaction_id),
        session_id=SESSION,
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        status=InteractionStatus.RUNNING,
        state=SessionState.EXECUTING,
        input_text="我这个月的个人产量是多少？",
        capability_id=None,
        clarification_rounds=0,
        last_event_sequence=2,
        error_category=None,
        created_at=updated_at - timedelta(seconds=100),
        updated_at=updated_at,
        completed_at=None,
    )
    return InteractionId(interaction_id)
