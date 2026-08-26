from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.intent import CapabilityCatalog, CapabilitySpec
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import CapabilityId, Role
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
HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"}
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


def overrides(
    store: InMemoryInteractionStore, runner: RecordingCapabilityRunner
) -> DependencyOverrides:
    member = membership("user-a", "tenant-a", "emp-1", Role.EMPLOYEE)
    return DependencyOverrides(
        model=ScriptedModelGateway(contents=[INTENT_PAYLOAD]),
        clock=FrozenClock(NOW),
        authorization=AuthorizationService(
            memberships=FakeMembershipSource(
                memberships_by_credential={("tenant-a", "user-a"): member}
            ),
            organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
            versions=FixedScopeVersionAssigner(),
        ),
        interactions=store,
        capability_runner=runner,
        capability_catalog=CATALOG,
        new_id=SequentialIds(),
    )


def client(store: InMemoryInteractionStore, runner: RecordingCapabilityRunner) -> httpx.AsyncClient:
    app = create_app(FactoryAgentSettings(environment="test"), overrides(store, runner))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


def sse_events(body: str) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        events.append((int(lines["id"]), lines["event"]))
    return events


@pytest.mark.asyncio
async def test_start_then_stream_produces_a_complete_event_sequence() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        assert created.status_code == 201
        interaction_id = created.json()["interaction_id"]

        stream = await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)

    assert stream.headers["content-type"].startswith("text/event-stream")
    events = sse_events(stream.text)
    assert events[0][1] == "interaction.started"
    assert events[-1][1] == "interaction.completed"
    assert [sequence for sequence, _ in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_reconnect_with_last_event_id_does_not_repeat_the_fetch() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        interaction_id = created.json()["interaction_id"]
        first = await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)
        resumed = await http.get(
            f"/v1/interactions/{interaction_id}/stream",
            headers={**HEADERS, "Last-Event-ID": "2"},
        )

    assert len(runner.requests) == 1
    assert [sequence for sequence, _ in sse_events(resumed.text)] == [
        sequence for sequence, _ in sse_events(first.text)
    ][2:]


@pytest.mark.asyncio
async def test_missing_identity_headers_are_unauthorized() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.post(
            "/v1/sessions/session-1/interactions", json={"text": "上个月产量"}
        )

    assert response.status_code == 401
    assert store.interactions == {}


@pytest.mark.asyncio
async def test_request_body_cannot_supply_tenant_or_user() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量", "tenant_id": "tenant-b", "user_id": "user-b"},
            headers=HEADERS,
        )

    assert response.status_code == 201
    record = store.interactions[response.json()["interaction_id"]]
    assert str(record.tenant_id) == "tenant-a"
    assert str(record.user_id) == "user-a"


@pytest.mark.asyncio
async def test_another_users_interaction_is_not_found() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        interaction_id = created.json()["interaction_id"]
        response = await http.post(
            f"/v1/interactions/{interaction_id}/cancel",
            headers={TENANT_HEADER: "tenant-a", USER_HEADER: "user-b"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unknown_interaction_returns_the_same_not_found() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.post("/v1/interactions/does-not-exist/cancel", headers=HEADERS)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cancel_produces_a_single_cancelled_terminal_event() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        interaction_id = created.json()["interaction_id"]
        cancelled = await http.post(f"/v1/interactions/{interaction_id}/cancel", headers=HEADERS)
        stream = await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)

    assert cancelled.json()["status"] == "cancelled"
    assert [name for _, name in sse_events(stream.text)] == ["interaction.cancelled"]
    assert runner.requests == []


@pytest.mark.asyncio
async def test_messages_are_listed_only_for_the_owning_user() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        owned = await http.get("/v1/sessions/session-1/messages", headers=HEADERS)
        foreign = await http.get(
            "/v1/sessions/session-1/messages",
            headers={TENANT_HEADER: "tenant-a", USER_HEADER: "user-b"},
        )

    assert [item["text"] for item in owned.json()["items"]] == ["上个月产量"]
    assert foreign.status_code == 403


@pytest.mark.asyncio
async def test_health_still_reports_dependency_readiness() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.get("/health/ready")

    assert response.json()["dependencies"]["model"] == "fake"
