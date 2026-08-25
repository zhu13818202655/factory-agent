"""Every usage event the application actually produces must satisfy the v1 schemas.

``test_usage_event_schemas`` validates hand-written examples against the
contract. This module runs real session pipelines and validates whatever they
enqueue, so a field added in application code cannot silently escape the
allowlist or carry a sensitive canary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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
from factory_agent.domain import CapabilityId, EmployeeId, Role, SessionId, TenantId, UserId
from factory_agent.ports import ModelErrorCategory, ModelGatewayError, UsageOutboxEvent
from factory_agent.ports.contracts import TrustedCredential
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    FakeScopeSource,
    membership,
)
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
    SequentialIds,
)

CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "usage-events" / "v1"
NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
VALID_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)
SESSION = SessionId("session-1")

#: Values that must never leave the application inside a usage event.
CANARIES = (
    "员工 E-CANARY 上月工资 8213.44 元",
    "emp-1",
    "dept-1",
    "user-a",
)

SCHEMA_BY_EVENT_TYPE = {
    "interaction_started": "interaction",
    "interaction_completed": "interaction",
    "llm_call_completed": "llm",
    "mes_call_completed": "mes",
    "artifact_generated": "artifact",
    "artifact_downloaded": "artifact",
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


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_ROOT / f"{name}.schema.json").read_text(encoding="utf-8"))


def validator_for(event_type: str) -> Draft202012Validator:
    schema = load_schema(SCHEMA_BY_EVENT_TYPE[event_type])
    schema["allOf"][0] = load_schema("envelope")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def credential() -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))


def authorization(role: Role) -> AuthorizationService:
    member = membership(
        "m-1", "user-a", "tenant-a", "emp-1", role, dept_ids=("dept-1",), valid_from=VALID_FROM
    )
    return AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={("tenant-a", "user-a"): [member]}
        ),
        assignments=FakeOrganizationSource(assignments_by_employee={"emp-1": (("dept-1",),)}),
        scopes=FakeScopeSource(
            scopes_by_membership={"m-1": ((frozenset({EmployeeId("emp-1")}), frozenset()),)}
        ),
        versions=FixedScopeVersionAssigner(),
    )


async def run_pipeline(
    payload: str,
    *,
    role: Role = Role.EMPLOYEE,
    failure: Exception | None = None,
    cancel: bool = False,
) -> list[UsageOutboxEvent]:
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
async def test_produced_events_validate_against_the_v1_schemas(scenario: str) -> None:
    events = await run_pipeline(**PIPELINES[scenario])

    assert events, "every terminal pipeline must produce at least one usage event"
    for event in events:
        event_type = event.payload["event_type"]
        assert isinstance(event_type, str)
        validator_for(event_type).validate(event.payload)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(PIPELINES))
async def test_produced_events_never_carry_a_canary(scenario: str) -> None:
    events = await run_pipeline(**PIPELINES[scenario])

    for event in events:
        serialized = json.dumps(event.payload, ensure_ascii=False, default=str)
        for canary in CANARIES:
            assert canary not in serialized


@pytest.mark.asyncio
async def test_every_event_id_is_unique_so_ingest_can_deduplicate() -> None:
    events = await run_pipeline(COMPLETE)

    event_ids = [event.event_id for event in events]

    assert len(event_ids) == len(set(event_ids))
