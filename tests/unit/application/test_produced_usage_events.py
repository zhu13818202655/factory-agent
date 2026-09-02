"""Field hygiene of the usage events the application actually produces.

The archive-payload format is owned locally by ``factory_agent.application.usage``
(``SCHEMA_VERSION``); the cross-service contract directory was deleted. These
tests run real session pipelines and guard the two properties
that used to be enforced by the contract schemas:

1. every produced event's payload keys stay inside the whitelisted field set,
   so a field added in application code cannot silently escape the allowlist;
2. no event carries a sensitive canary (question text, employee id, ...).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.intent import (
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
)
from factory_agent.application.session import SessionService, StartRequest
from factory_agent.domain import CapabilityId, Role, SessionId, TenantId, UserId
from factory_agent.ports import ModelErrorCategory, ModelGatewayError, UsageEvent
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
    )
)

COMPLETE = '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
OWNER_ONLY = (
    '{"capability_id": "FR-011", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
)
INCOMPLETE = '{"capability_id": null, "confidence": 0.2, "slots": {}}'


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
) -> list[UsageEvent]:
    store = InMemoryInteractionStore()
    gateway = ScriptedModelGateway(contents=[payload], failures=[failure] if failure else [])
    service = SessionService(
        store,
        authorization(role),
        CapabilityIntentParser(
            gateway, CATALOG, model_alias="factory-fast", timezone_name="Asia/Shanghai"
        ),
        RecordingCapabilityRunner(),
        FrozenClock(NOW),
        new_id=SequentialIds(),
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
