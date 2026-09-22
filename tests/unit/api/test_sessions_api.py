

import json
from datetime import datetime, timezone
from types import SimpleNamespace

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
from factory_agent.domain import (
    CapabilityId,
    ConversationRecord,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    MessageId,
    MessageKind,
    MessageRecord,
    MessageRole,
    Role,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
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
USER_B_HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-b"}
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
    store: InMemoryInteractionStore,
    runner: RecordingCapabilityRunner,
    *,
    role: Role = Role.EMPLOYEE,
    extra_members: dict[tuple[str, str], object] | None = None,
) -> DependencyOverrides:
    member = membership("user-a", "tenant-a", "emp-1", role)
    members = {("tenant-a", "user-a"): member}
    if extra_members:
        members.update(extra_members)  # type: ignore[arg-type]
    return DependencyOverrides(
        model=ScriptedModelGateway(contents=[INTENT_PAYLOAD]),
        clock=FrozenClock(NOW),
        authorization=AuthorizationService(
            memberships=FakeMembershipSource(memberships_by_credential=members),
            organizations=FakeOrganizationSource(
                depts_by_employee={"emp-1": ("dept-1",), "emp-2": ("dept-1",)}
            ),
            versions=FixedScopeVersionAssigner(),
        ),
        interactions=store,
        capability_runner=runner,
        capability_catalog=CATALOG,
        new_id=SequentialIds(),
    )


def client(
    store: InMemoryInteractionStore,
    runner: RecordingCapabilityRunner,
    *,
    role: Role = Role.EMPLOYEE,
) -> httpx.AsyncClient:
    app = create_app(FactoryAgentSettings(environment="local"), overrides(store, runner, role=role))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


def foreign_client(
    store: InMemoryInteractionStore, runner: RecordingCapabilityRunner
) -> httpx.AsyncClient:
    """A second identity in the same tenant, to prove session isolation."""
    app = create_app(
        FactoryAgentSettings(environment="local"),
        overrides(
            store,
            runner,
            extra_members={
                ("tenant-a", "user-b"): membership("user-b", "tenant-a", "emp-2", Role.EMPLOYEE)
            },
        ),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid")


def seed_conversation(
    store: InMemoryInteractionStore,
    session_id: str,
    *,
    updated_at: datetime = NOW,
    created_at: datetime = NOW,
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
) -> None:
    """Write a conversation row directly, to control recency without a clock."""
    store.conversations[session_id] = ConversationRecord(
        session_id=SessionId(session_id),
        tenant_id=TenantId(tenant_id),
        user_id=UserId(user_id),
        created_at=created_at,
        updated_at=updated_at,
    )


def seed_message(
    store: InMemoryInteractionStore,
    message_id: str,
    session_id: str,
    *,
    text: str,
    role: MessageRole = MessageRole.USER,
    kind: MessageKind = MessageKind.PLAIN_TEXT,
    created_at: datetime = NOW,
    sequence: int = 1,
    interaction_id: str = "it-1",
) -> None:
    store.messages.append(
        MessageRecord(
            message_id=MessageId(message_id),
            interaction_id=InteractionId(interaction_id),
            session_id=SessionId(session_id),
            tenant_id=TenantId("tenant-a"),
            user_id=UserId("user-a"),
            role=role,
            kind=kind,
            sequence=sequence,
            text=text,
            payload={},
            created_at=created_at,
        )
    )


def seed_turn(
    store: InMemoryInteractionStore,
    interaction_id: str,
    session_id: str,
    *,
    status: InteractionStatus = InteractionStatus.COMPLETED,
    created_at: datetime = NOW,
) -> None:
    store.interactions[interaction_id] = InteractionRecord(
        interaction_id=InteractionId(interaction_id),
        session_id=SessionId(session_id),
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        status=status,
        state=SessionState.ANSWERED,
        input_text="上个月产量",
        capability_id=None,
        clarification_rounds=0,
        last_event_sequence=1,
        error_category=None,
        created_at=created_at,
        updated_at=created_at,
        completed_at=created_at,
    )


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
async def test_a_foreign_session_returns_an_empty_page_not_an_error() -> None:
    """An authenticated non-owner sees nothing, and learns nothing.

    ``test_messages_are_listed_only_for_the_owning_user`` covers a rejected
    credential (403). This covers the subtler case: user-b is a real member of
    the tenant, so authorization passes and the ownership filter in the query is
    the only thing keeping another user's messages out. An empty page — rather
    than a 403/404 — is what makes "exists but not yours" indistinguishable from
    "never existed".
    """
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        async with foreign_client(store, runner) as foreign_http:
            foreign = await foreign_http.get(
                "/v1/sessions/session-1/messages", headers=USER_B_HEADERS
            )

    assert foreign.status_code == 200
    assert foreign.json()["items"] == []
    # This legacy route serialises with ``exclude_none``, so a null ``next_cursor``
    # is absent from the body rather than present-and-null (the conversation
    # endpoints always send the key; see ``test_conversation_detail_*``).
    assert foreign.json().get("next_cursor") is None


@pytest.mark.asyncio
async def test_health_still_reports_dependency_readiness() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.get("/health/ready")

    assert response.json()["dependencies"]["model"] == "fake"


async def _exported_artifact_id(
    http: httpx.AsyncClient,
) -> str:
    created = await http.post(
        "/v1/sessions/session-1/interactions",
        json={"text": "上个月产量"},
        headers=HEADERS,
    )
    interaction_id = created.json()["interaction_id"]
    stream = await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)
    body = next(
        data
        for data in _event_data(stream.text)
        if '"capability_id"' in data and '"artifact_id"' in data
    )
    return json.loads(body)["artifact_id"]


def _event_data(body: str) -> list[str]:
    return [
        frame.split("data: ", 1)[1]
        for frame in body.split("\n\n")
        if frame.strip() and "data: " in frame
    ]


@pytest.mark.asyncio
async def test_export_download_streams_xlsx_after_ownership_validation() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        artifact_id = await _exported_artifact_id(http)
        response = await http.get(f"/v1/artifacts/{artifact_id}/download", headers=HEADERS)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert "attachment" in response.headers["content-disposition"]
        assert response.content[:2] == b"PK"


@pytest.mark.asyncio
async def test_export_download_is_owned_and_not_replayable_across_users() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    member_b = membership("user-b", "tenant-a", "emp-2", Role.EMPLOYEE)

    async with client(store, runner) as http:
        artifact_id = await _exported_artifact_id(http)
        # A valid but different user cannot fetch the export (indistinguishable
        # from a missing id).
        app = create_app(
            FactoryAgentSettings(environment="local"),
            overrides(
                store,
                runner,
                extra_members={("tenant-a", "user-b"): member_b},
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
        ) as foreign_http:
            foreign = await foreign_http.get(
                f"/v1/artifacts/{artifact_id}/download",
                headers={TENANT_HEADER: "tenant-a", USER_HEADER: "user-b"},
            )
        missing = await http.get("/v1/artifacts/does-not-exist/download", headers=HEADERS)

    assert foreign.status_code == 404
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_drill_body_rejects_a_half_supplied_window() -> None:
    """窗口两半必须成对下发：只发一半是客户端 bug，422 拒掉而不是猜另一半（D-7）."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.post(
            "/v1/sessions/session-1/interactions",
            json={
                "text": "查看该车间工资",
                "drill": {
                    "capability_id": "fr008_payroll_ranking",
                    "dept_ids": ["dept-1"],
                    "time_range_start": "2026-08-09T16:00:00+00:00",
                },
            },
            headers=HEADERS,
        )

    assert response.status_code == 422
    assert store.interactions == {}


@pytest.mark.asyncio
async def test_drill_body_accepts_a_complete_window() -> None:
    """成对的窗口载荷通过校验并落到下钻请求上（不在请求体里带任何身份字段）."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    runner.recipes = SimpleNamespace(capability_ids=frozenset({"fr008_payroll_ranking"}))
    body = {
        "text": "查看该车间工资",
        "drill": {
            "capability_id": "fr008_payroll_ranking",
            "dept_ids": ["dept-1"],
            "time_range_start": "2026-08-09T16:00:00+00:00",
            "time_range_end": "2026-08-13T16:00:00+00:00",
        },
    }

    # 下钻载荷是进程内状态：发起与领取流必须走同一个应用实例（单 worker 部署），
    # 换一个实例会把它当作「从未被领取的 pending」，本轮就退回纯文本路径了。
    async with client(store, runner, role=Role.MANAGER) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions", json=body, headers=HEADERS
        )
        assert created.status_code == 201
        interaction_id = created.json()["interaction_id"]
        await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)

    assert len(runner.requests) == 1
    # 发到能力层的必须是回传的那个窗口，而不是表述解析出来的「本月」。
    assert runner.requests[0].time_range.start == datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc)
    assert runner.requests[0].time_range.end == datetime(2026, 8, 13, 16, 0, tzinfo=timezone.utc)


# --- Conversations: list / create / detail ---------------------------------


@pytest.mark.asyncio
async def test_a_conversation_created_before_its_first_question_is_listed() -> None:
    """会话表的全部意义：空会话也要出现在历史列表里（否则前端「新对话」即消失）."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post("/v1/conversations", headers=HEADERS)
        listed = await http.get("/v1/conversations", headers=HEADERS)

    assert created.status_code == 201
    body = created.json()
    assert body["interactions"] == []
    assert body["messages"] == []
    assert body["conversation"]["interaction_count"] == 0
    assert body["conversation"]["message_count"] == 0
    assert body["conversation"]["title"] is None
    assert body["conversation"]["last_message"] is None
    assert body["conversation"]["last_status"] is None

    items = listed.json()["items"]
    assert [item["session_id"] for item in items] == [body["conversation"]["session_id"]]


@pytest.mark.asyncio
async def test_conversation_title_is_the_first_question_truncated_to_thirty_chars() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    long_question = "帮我看一下这个月整个后道车间所有工序的完工数量和不良品明细汇总"
    seed_conversation(store, "session-1")
    seed_message(store, "m-1", "session-1", text=long_question)
    seed_message(
        store,
        "m-2",
        "session-1",
        text="那上个月呢",
        created_at=datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc),
        sequence=2,
    )

    async with client(store, runner) as http:
        listed = await http.get("/v1/conversations", headers=HEADERS)

    title = listed.json()["items"][0]["title"]
    assert title == f"{long_question[:30]}…"
    assert len(title) == 31


@pytest.mark.asyncio
async def test_conversation_list_is_newest_first_and_pages_with_a_cursor() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    seed_conversation(store, "s-oldest", updated_at=NOW.replace(day=20))
    seed_conversation(store, "s-middle", updated_at=NOW.replace(day=22))
    seed_conversation(store, "s-newest", updated_at=NOW.replace(day=24))

    async with client(store, runner) as http:
        first = await http.get("/v1/conversations?limit=2", headers=HEADERS)
        cursor = first.json()["next_cursor"]
        second = await http.get(f"/v1/conversations?limit=2&cursor={cursor}", headers=HEADERS)

    assert [item["session_id"] for item in first.json()["items"]] == ["s-newest", "s-middle"]
    assert cursor is not None
    assert [item["session_id"] for item in second.json()["items"]] == ["s-oldest"]
    assert second.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_conversation_list_returns_an_empty_page_rather_than_an_error() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.get("/v1/conversations", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_conversations_are_invisible_to_another_identity() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    seed_conversation(store, "session-1")
    seed_conversation(store, "session-other-tenant", tenant_id="tenant-b", user_id="user-a")

    async with client(store, runner) as http:
        async with foreign_client(store, runner) as foreign_http:
            detail = await foreign_http.get("/v1/conversations/session-1", headers=USER_B_HEADERS)
            listed = await foreign_http.get("/v1/conversations", headers=USER_B_HEADERS)
        owned = await http.get("/v1/conversations", headers=HEADERS)

    assert detail.status_code == 404
    assert detail.json()["detail"] == "conversation not found"
    assert listed.json()["items"] == []
    assert [item["session_id"] for item in owned.json()["items"]] == ["session-1"]


@pytest.mark.asyncio
async def test_create_conversation_is_idempotent_and_never_clears_history() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    body = {"session_id": "client-generated-1"}

    async with client(store, runner) as http:
        first = await http.post("/v1/conversations", json=body, headers=HEADERS)
        seed_message(store, "m-1", "client-generated-1", text="上个月产量")
        seed_turn(store, "it-1", "client-generated-1")
        again = await http.post("/v1/conversations", json=body, headers=HEADERS)

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["conversation"]["session_id"] == "client-generated-1"
    assert again.json()["conversation"]["title"] == "上个月产量"
    assert len(again.json()["messages"]) == 1
    assert len(store.conversations) == 1


@pytest.mark.asyncio
async def test_create_conversation_rejects_a_malformed_session_id() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.post(
            "/v1/conversations", json={"session_id": "bad/id with spaces"}, headers=HEADERS
        )

    assert response.status_code == 422
    assert store.conversations == {}


@pytest.mark.asyncio
async def test_create_conversation_stops_at_the_per_user_cap_but_replays_known_ones() -> None:
    """上限只拦「新建」；已达上限时，既有会话仍然可以正常打开."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    for index in range(500):
        seed_conversation(store, f"s-{index:03d}")

    async with client(store, runner) as http:
        rejected = await http.post(
            "/v1/conversations", json={"session_id": "s-500"}, headers=HEADERS
        )
        replayed = await http.post(
            "/v1/conversations", json={"session_id": "s-000"}, headers=HEADERS
        )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "conversation limit reached"
    assert "s-500" not in store.conversations
    assert replayed.status_code == 200
    assert len(store.conversations) == 500


@pytest.mark.asyncio
async def test_asking_a_question_lists_the_conversation_without_creating_one() -> None:
    """老前端不调建档接口也必须在列表里看到自己的会话（向后兼容路径）."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        assert created.status_code == 201
        listed = await http.get("/v1/conversations", headers=HEADERS)
        detail = await http.get("/v1/conversations/session-1", headers=HEADERS)

    item = listed.json()["items"][0]
    assert item["session_id"] == "session-1"
    assert item["title"] == "上个月产量"
    assert item["interaction_count"] == 1
    assert item["last_message"]["role"] == "user"
    assert item["last_status"] == "pending"
    assert [turn["status"] for turn in detail.json()["interactions"]] == ["pending"]


@pytest.mark.asyncio
async def test_conversation_detail_carries_the_whole_flow() -> None:
    """一轮问答跑完后，详情要给出「轮次 + 消息」的完整会话流骨架."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "上个月产量"},
            headers=HEADERS,
        )
        interaction_id = created.json()["interaction_id"]
        await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)
        detail = await http.get("/v1/conversations/session-1", headers=HEADERS)

    body = detail.json()
    assert body["conversation"]["session_id"] == "session-1"
    assert body["conversation"]["last_status"] == "completed"
    assert [turn["interaction_id"] for turn in body["interactions"]] == [interaction_id]
    assert body["interactions"][0]["capability_id"] == "FR-001"
    # 消息带轮次归属与时间戳：前端靠它把消息归回每一轮（主文档 §4.5 的加法式补字段）。
    assert {message["interaction_id"] for message in body["messages"]} == {interaction_id}
    assert all(message["created_at"] for message in body["messages"])


@pytest.mark.asyncio
async def test_conversation_detail_can_exclude_message_kinds() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    seed_conversation(store, "session-1")
    seed_message(store, "m-1", "session-1", text="上个月产量")
    seed_message(
        store,
        "m-2",
        "session-1",
        text="正在取数",
        role=MessageRole.ASSISTANT,
        kind=MessageKind.PHASE,
        sequence=2,
    )

    async with client(store, runner) as http:
        everything = await http.get("/v1/conversations/session-1", headers=HEADERS)
        filtered = await http.get(
            "/v1/conversations/session-1?exclude_kinds=phase,thinking", headers=HEADERS
        )
        invalid = await http.get(
            "/v1/conversations/session-1?exclude_kinds=phase,nonsense", headers=HEADERS
        )

    assert [item["kind"] for item in everything.json()["messages"]] == ["plain_text", "phase"]
    assert [item["kind"] for item in filtered.json()["messages"]] == ["plain_text"]
    # 计数口径与过滤参数无关：过程行本来就不计入。
    assert everything.json()["conversation"]["message_count"] == 1
    assert filtered.json()["conversation"]["message_count"] == 1
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_conversation_detail_is_not_found_for_an_unknown_session() -> None:
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        response = await http.get("/v1/conversations/does-not-exist", headers=HEADERS)

    assert response.status_code == 404
    assert response.json()["detail"] == "conversation not found"


@pytest.mark.asyncio
async def test_conversation_limits_are_clamped_instead_of_rejected() -> None:
    """limit 是客户端的偏好，不是边界：越界由服务端收敛，不报 422."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()
    seed_conversation(store, "session-1")

    async with client(store, runner) as http:
        listed = await http.get("/v1/conversations?limit=100000", headers=HEADERS)
        detail = await http.get("/v1/conversations/session-1?limit=0", headers=HEADERS)

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert len(listed.json()["items"]) == 1
    assert detail.json()["messages"] == []


@pytest.mark.asyncio
async def test_an_unknown_conversation_is_never_created_by_a_detail_read() -> None:
    """详情是纯读：不能因为一次 GET 就凭空建出会话行."""
    store, runner = InMemoryInteractionStore(), RecordingCapabilityRunner()

    async with client(store, runner) as http:
        await http.get("/v1/conversations/session-ghost", headers=HEADERS)

    assert store.conversations == {}
