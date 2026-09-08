


from datetime import datetime, timedelta, timezone

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.intent import CapabilityCatalog, CapabilityIntentParser
from factory_agent.application.session import SessionLimits, SessionService
from factory_agent.domain import (
    INTERACTION_FAILED,
    INTERACTION_HEARTBEAT,
    INTERACTION_PHASE,
    INTERACTION_STARTED,
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
from factory_agent.ports import InteractionOwner
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
OWNER = InteractionOwner(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))

_EMPTY_CATALOG = CapabilityCatalog(specs=())


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


def make_service(
    store: InMemoryInteractionStore, limits: SessionLimits
) -> SessionService:
    parser = CapabilityIntentParser(
        ScriptedModelGateway(),  # pyright: ignore[reportArgumentType]
        _EMPTY_CATALOG,
        model_alias="factory-fast",
        timezone_name="Asia/Shanghai",
    )
    return SessionService(
        store,
        authorization(),
        parser,
        RecordingCapabilityRunner(),
        FrozenClock(NOW),
        new_id=SequentialIds(),
        limits=limits,
        sleep=_no_sleep,
    )


def seed_running(
    store: InMemoryInteractionStore, *, updated_at: datetime
) -> InteractionId:
    """A run whose executor connection died mid-execution."""
    interaction_id = InteractionId("i-orphan")
    store.interactions[str(interaction_id)] = InteractionRecord(
        interaction_id=interaction_id,
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
    store.events[str(interaction_id)] = [
        SessionEvent(sequence=1, name=INTERACTION_STARTED, data={}),
        SessionEvent(sequence=2, name=INTERACTION_PHASE, data={}),
    ]
    return interaction_id


async def drain(
    service: SessionService, interaction_id: InteractionId, *, after_sequence: int = 0
) -> list[SessionEvent]:
    stream = service.stream(credential(), interaction_id, after_sequence=after_sequence)
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_orphaned_running_interaction_is_failed_on_reconnect() -> None:
    store = InMemoryInteractionStore()
    service = make_service(store, SessionLimits(stale_running_seconds=600))
    interaction_id = seed_running(store, updated_at=NOW - timedelta(seconds=700))

    events = await drain(service, interaction_id, after_sequence=2)

    terminal = events[-1]
    assert terminal.name == INTERACTION_FAILED
    assert terminal.data["error_category"] == "executor_lost"
    assert terminal.sequence == 3
    healed = store.interactions[str(interaction_id)]
    assert healed.status is InteractionStatus.FAILED
    assert healed.state is SessionState.FAILED
    assert healed.completed_at == NOW
    assert healed.last_event_sequence == 3
    assert [m.kind for m in store.messages] == [MessageKind.ERROR]
    assert store.usage_events  # completion usage event was metered


@pytest.mark.asyncio
async def test_fresh_running_interaction_is_never_healed() -> None:
    store = InMemoryInteractionStore()
    service = make_service(store, SessionLimits(stale_running_seconds=600))
    interaction_id = seed_running(store, updated_at=NOW - timedelta(seconds=10))

    await drain(service, interaction_id, after_sequence=2)

    assert store.interactions[str(interaction_id)].status is InteractionStatus.RUNNING
    assert store.messages == []


@pytest.mark.asyncio
async def test_follow_budget_exhaustion_emits_wire_only_terminal() -> None:
    store = InMemoryInteractionStore()
    limits = SessionLimits(
        heartbeat_seconds=0.01, follow_timeout_seconds=0.02, stale_running_seconds=600
    )
    service = make_service(store, limits)
    interaction_id = seed_running(store, updated_at=NOW - timedelta(seconds=10))

    events = await drain(service, interaction_id, after_sequence=2)

    assert events
    assert events[-1].name == INTERACTION_FAILED
    assert events[-1].data["error_category"] == "follow_timeout"
    # Wire-only terminal sits one past the last persisted event so the
    # frontend's id-based dedup lets it through.
    assert events[-1].sequence == 3
    assert {e.name for e in events[:-1]} == {INTERACTION_HEARTBEAT}
    # The interaction row is untouched: a genuinely slow executor can still
    # persist its outcome.
    assert store.interactions[str(interaction_id)].status is InteractionStatus.RUNNING
    assert store.messages == []
