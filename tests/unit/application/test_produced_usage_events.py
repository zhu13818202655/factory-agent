"""Field hygiene of the usage events the application actually produces.

The archive-payload format is owned locally by ``factory_agent.application.usage``
(``SCHEMA_VERSION``); the cross-service contract directory was deleted. These
tests run real session pipelines and guard the two properties
that used to be enforced by the contract schemas:

1. every produced event's payload keys stay inside the whitelisted field set,
   so a field added in application code cannot silently escape the allowlist;
2. no event carries a sensitive canary (question text, employee id, ...).
"""



import json
from datetime import datetime, timezone
from typing import Any, Literal

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.chitchat import ChatResponder
from factory_agent.application.intent import (
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
)
from factory_agent.application.scope_guard import ScopeGuard
from factory_agent.application.session import SessionService, StartRequest
from factory_agent.application.summary import ResultSummarizer
from factory_agent.domain import CapabilityId, Role, SessionId, TenantId, UserId
from factory_agent.ports import Clock, ModelErrorCategory, ModelGatewayError, UsageEvent
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
    TickingClock,
)

#: Which carrier the scope guard runs under (mirrors ``intent``'s parameter).
ScopeGuardMode = Literal["merged", "dedicated"]

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
SESSION = SessionId("session-1")

#: Values that must never leave the application inside a usage event.
CANARIES = (
    "员工 E-CANARY 上月工资 8213.44 元",
    "emp-1",
    "dept-1",
    "user-a",
)

#: Envelope fields shared by every event (mirror of ``UsageContext.envelope``).
ENVELOPE_FIELDS = frozenset(
    {
        "event_id",
        "schema_version",
        "occurred_at",
        "tenant_id",
        "user_subject_id",
        "session_id",
        "interaction_id",
        "trace_id",
        "event_type",
    }
)

#: Whitelisted fields per event type (mirror of ``application/usage.py``).
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "interaction_started": ENVELOPE_FIELDS | {"capability", "entrypoint", "role_category"},
    "interaction_completed": ENVELOPE_FIELDS
    | {
        "status",
        "duration_ms",
        "mes_duration_ms",
        "llm_duration_ms",
        "local_duration_ms",
        "result_rows_bucket",
        "error_category",
    },
    "llm_call_completed": ENVELOPE_FIELDS
    | {
        "logical_call_id",
        "stage",
        "model_alias",
        "actual_model",
        "attempt",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "duration_ms",
        "status",
        "fallback_reason",
        "error_category",
        # Merged EXTRACT carrier only: whether the call was asked for the scope
        # verdict, and the verdict it returned (``null`` when it returned none).
        "includes_scope",
        "scope_verdict",
    },
    "mes_call_completed": ENVELOPE_FIELDS
    | {"operation_id", "page_count", "row_count_bucket", "duration_ms", "status", "error_category"},
}

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
            capability_id=CapabilityId("chitchat"),
            title="闲聊与常识问答",
            required_slots=(),
        ),
    )
)

COMPLETE = '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
OWNER_ONLY = (
    '{"capability_id": "FR-011", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
)
INCOMPLETE = '{"capability_id": null, "confidence": 0.2, "slots": {}}'
CHITCHAT = '{"capability_id": "chitchat", "confidence": 0.95, "slots": {}}'


def credential() -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))


def authorization(role: Role) -> AuthorizationService:
    member = membership("user-a", "tenant-a", "emp-1", role)
    return AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={("tenant-a", "user-a"): member}
        ),
        organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
        versions=FixedScopeVersionAssigner(),
    )


async def run_pipeline(
    payload: str,
    *,
    role: Role = Role.EMPLOYEE,
    failure: Exception | None = None,
    cancel: bool = False,
    chat_text: str | None = None,
    clock: Clock | None = None,
) -> list[UsageEvent]:
    store = InMemoryInteractionStore()
    gateway = ScriptedModelGateway(contents=[payload], failures=[failure] if failure else [])
    chat = None
    if chat_text is not None:
        chat = ChatResponder(
            ScriptedModelGateway(contents=[chat_text]), model_alias="factory-summary"
        )
    service = SessionService(
        store,
        authorization(role),
        CapabilityIntentParser(
            gateway, CATALOG, model_alias="factory-fast", timezone_name="Asia/Shanghai"
        ),
        RecordingCapabilityRunner(),
        clock or FrozenClock(NOW),
        new_id=SequentialIds(),
        chat=chat,
    )
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    if cancel:
        await service.cancel(credential(), record.interaction_id)
    else:
        async for _ in service.stream(credential(), record.interaction_id):
            pass
    return store.usage_events


PIPELINES: dict[str, dict[str, Any]] = {
    "completed": {"payload": COMPLETE},
    "clarifying": {"payload": INCOMPLETE},
    "rejected": {"payload": OWNER_ONLY, "role": Role.EMPLOYEE},
    "chat": {"payload": CHITCHAT, "chat_text": "你好呀！有什么可以帮你？"},
    "gateway_failure": {
        "payload": COMPLETE,
        "failure": ModelGatewayError(ModelErrorCategory.TIMEOUT, "upstream timed out"),
    },
    "cancelled": {"payload": COMPLETE, "cancel": True},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(PIPELINES))
async def test_produced_events_stay_inside_the_whitelisted_field_set(scenario: str) -> None:
    events = await run_pipeline(**PIPELINES[scenario])

    assert events, "every terminal pipeline must produce at least one usage event"
    for event in events:
        event_type = event.payload["event_type"]
        assert isinstance(event_type, str)
        assert event_type in ALLOWED_FIELDS, f"unexpected event type {event_type!r}"
        unknown = set(event.payload) - ALLOWED_FIELDS[event_type]
        assert not unknown, f"{event_type} carries unapproved fields: {sorted(unknown)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(PIPELINES))
async def test_produced_events_never_carry_a_canary(scenario: str) -> None:
    events = await run_pipeline(**PIPELINES[scenario])

    for event in events:
        serialized = json.dumps(event.payload, ensure_ascii=False, default=str)
        for canary in CANARIES:
            assert canary not in serialized


@pytest.mark.asyncio
async def test_every_event_id_is_unique_for_idempotent_writes() -> None:
    events = await run_pipeline(COMPLETE)

    event_ids = [event.event_id for event in events]

    assert len(event_ids) == len(set(event_ids))


def completion_payload(events: list[UsageEvent]) -> dict[str, object]:
    completion = next(event for event in events if event.event_type == "interaction_completed")
    return completion.payload


@pytest.mark.asyncio
async def test_completion_event_splits_model_and_mes_time_out_of_the_total() -> None:
    """``local_duration_ms`` is the residual the other two columns do not cover."""
    payload = completion_payload(await run_pipeline(COMPLETE, clock=TickingClock()))

    llm = payload["llm_duration_ms"]
    mes = payload["mes_duration_ms"]
    total = payload["duration_ms"]
    local = payload["local_duration_ms"]
    assert isinstance(llm, int)
    assert isinstance(mes, int)
    assert isinstance(total, int)
    assert isinstance(local, int)

    assert llm > 0, "the EXTRACT call must be metered"
    assert mes == 7, "RecordingCapabilityRunner reports a fixed 7 ms"
    assert llm + mes <= total
    assert local == total - llm - mes


@pytest.mark.asyncio
async def test_a_zero_length_run_clamps_the_local_residual() -> None:
    """A frozen clock makes the total exactly time the parts already cover."""
    payload = completion_payload(await run_pipeline(COMPLETE))

    llm = payload["llm_duration_ms"]
    assert isinstance(llm, int)

    assert payload["duration_ms"] == 0
    assert llm > 0
    assert payload["local_duration_ms"] == 0


@pytest.mark.asyncio
async def test_an_aborted_run_still_reports_the_model_time_it_spent() -> None:
    """A clarification made no MES call but did pay for the EXTRACT round trip."""
    payload = completion_payload(await run_pipeline(INCOMPLETE, clock=TickingClock()))

    llm = payload["llm_duration_ms"]
    assert isinstance(llm, int)

    assert payload["status"] == "completed"
    assert llm > 0
    assert payload["mes_duration_ms"] == 0


#: EXTRACT payload carrying the merged scope verdict.
COMPLETE_SCOPED = (
    '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"},'
    ' "scope": {"verdict": "within", "target": ""}}'
)
COMPLETE_SCOPED_BEYOND = (
    '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"},'
    ' "scope": {"verdict": "beyond", "target": "全组的工资明细"}}'
)
GUARD_WITHIN = '{"verdict": "within", "target": ""}'
GUARD_BEYOND = '{"verdict": "beyond", "target": "全组的工资明细"}'


async def run_guarded_pipeline(
    *,
    scope_guard_mode: ScopeGuardMode,
    intent_payload: str,
    guard_payload: str = GUARD_WITHIN,
) -> list[UsageEvent]:
    """A full business query with both the guard and the summarizer wired.

    The parser and the guard get separate scripted gateways so each call's
    script is independent of how many round trips the carrier needs.
    """
    store = InMemoryInteractionStore()
    service = SessionService(
        store,
        authorization(Role.EMPLOYEE),
        CapabilityIntentParser(
            ScriptedModelGateway(contents=[intent_payload]),
            CATALOG,
            model_alias="factory-fast",
            timezone_name="Asia/Shanghai",
            scope_guard_mode=scope_guard_mode,
        ),
        RecordingCapabilityRunner(),
        FrozenClock(NOW),
        new_id=SequentialIds(),
        summarizer=ResultSummarizer(
            ScriptedModelGateway(contents=["上个月合计 12 件。"]), model_alias="factory-summary"
        ),
        scope_guard=ScopeGuard(
            ScriptedModelGateway(contents=[guard_payload]), model_alias="factory-summary"
        ),
    )
    record = await service.start(credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    async for _ in service.stream(credential(), record.interaction_id):
        pass
    return store.usage_events


def stages_of(events: list[UsageEvent]) -> list[str]:
    return [
        str(event.payload["stage"]) for event in events if event.event_type == "llm_call_completed"
    ]


def extract_event(events: list[UsageEvent]) -> UsageEvent:
    return next(
        event
        for event in events
        if event.event_type == "llm_call_completed" and event.payload["stage"] == "extract"
    )


@pytest.mark.asyncio
async def test_merged_scope_verdict_costs_one_fewer_model_call() -> None:
    """The merged carrier returns capability and scope in one round trip."""
    events = await run_guarded_pipeline(scope_guard_mode="merged", intent_payload=COMPLETE_SCOPED)

    assert stages_of(events) == ["extract", "summarize"]

    extract = next(event for event in events if event.payload.get("includes_scope") is True)
    assert extract.payload["scope_verdict"] == {"verdict": "within"}


@pytest.mark.asyncio
async def test_dedicated_mode_keeps_the_independent_guard_call() -> None:
    """``dedicated`` reproduces the pre-merge chain and its event shape."""
    events = await run_guarded_pipeline(scope_guard_mode="dedicated", intent_payload=COMPLETE)

    assert stages_of(events) == ["extract", "scope_guard", "summarize"]

    extract = extract_event(events)
    assert "includes_scope" not in extract.payload
    assert "scope_verdict" not in extract.payload


@pytest.mark.asyncio
async def test_a_merged_payload_without_a_verdict_falls_back_to_the_guard_call() -> None:
    """No usable scope key means the dedicated call takes over, not a free pass."""
    events = await run_guarded_pipeline(scope_guard_mode="merged", intent_payload=COMPLETE)

    assert stages_of(events) == ["extract", "scope_guard", "summarize"]

    extract = extract_event(events)
    assert extract.payload["includes_scope"] is True
    assert extract.payload["scope_verdict"] is None


@pytest.mark.asyncio
async def test_a_merged_beyond_verdict_still_denies_before_any_business_call() -> None:
    """The merged carrier can only narrow access; it denies like the guard did."""
    events = await run_guarded_pipeline(
        scope_guard_mode="merged", intent_payload=COMPLETE_SCOPED_BEYOND
    )

    assert stages_of(events) == ["extract"]
    assert completion_payload(events)["status"] == "failed"
    assert completion_payload(events)["error_category"] == "scope_forbidden"
    assert not [event for event in events if event.event_type == "mes_call_completed"]


@pytest.mark.asyncio
async def test_a_dedicated_guard_denial_uses_the_same_outcome_shape() -> None:
    """Both carriers must deny through one path, so callers see one behaviour."""
    events = await run_guarded_pipeline(
        scope_guard_mode="dedicated", intent_payload=COMPLETE, guard_payload=GUARD_BEYOND
    )

    assert stages_of(events) == ["extract", "scope_guard"]
    assert completion_payload(events)["status"] == "failed"
    assert completion_payload(events)["error_category"] == "scope_forbidden"
    assert not [event for event in events if event.event_type == "mes_call_completed"]
