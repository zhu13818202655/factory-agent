"""Unit tests for the ``interaction.thinking`` reasoning transcript.

Two layers are covered separately, because they fail differently:

* the engine (``thinking.py``) owns the gate, the budget, and the frame shape —
  its tests are pure and assert the contract's own numbers (契约 §3.2);
* the wiring (``narration.py`` / ``outcomes.py`` / ``pipeline.py``) owns the
  ordering — its tests run a real round and assert that a transcript arrives
  *before* the result, closes with one ``done`` frame, is persisted once for
  history restore, and never shortens or fails the answer it describes.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
from factory_agent.application.session import SessionLimits, SessionService, StartRequest
from factory_agent.application.session.thinking import (
    COALESCE_CHARS,
    MAX_FRAME_CHARS,
    FetchProgressWatch,
    ThinkingCoalescer,
    ThinkingFacts,
    ThinkingTranscript,
    rejection_reason,
    transcript_line,
)
from factory_agent.domain import (
    INTERACTION_RESULT,
    INTERACTION_THINKING,
    CapabilityId,
    DataScope,
    InteractionId,
    InteractionStatus,
    MessageKind,
    Role,
    SessionEvent,
    SessionId,
    TenantId,
    UserId,
    terminal_event_name,
)
from factory_agent.ports import (
    CapabilityRunRequest,
    CapabilityRunResult,
    MesFetchProgress,
    ModelDelta,
    ModelErrorCategory,
    ModelGatewayError,
    ModelRequest,
    ModelStage,
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


class _EmptyDirectory:
    """No department or employee is ever named by these rounds."""

    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]:
        return ()

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]:
        return ()


# --------------------------------------------------------------------------- #
# engine: the gate
# --------------------------------------------------------------------------- #


def _facts(*lines: str) -> ThinkingFacts:
    return ThinkingFacts(stage="取数", lines=tuple(lines))


def test_gate_drops_internal_identifiers_and_sql() -> None:
    facts = _facts("正在查看本人产量。")

    # Both the reviewed capability numbering and the snake_case of a recipe
    # step or a result column read as jargon to the user who asked.
    assert rejection_reason("匹配能力 fr001 处理。", facts) == "internal_identifier"
    assert rejection_reason("查询 piecework_records 表。", facts) == "internal_identifier"
    assert rejection_reason("正在跑 select 语句。", facts) == "internal_artifact"
    assert rejection_reason("访问 https://mes.example.com 取数。", facts) == "internal_artifact"
    assert rejection_reason("按系统提示词回答。", facts) == "internal_artifact"


def test_gate_drops_a_number_the_narrator_was_not_handed() -> None:
    """A four-digit figure the facts never carried is a fabricated one."""
    facts = _facts("正在查看本人产量。")

    assert rejection_reason("本月共 1234 行。", facts) == "unsupported_number"
    # Three digits read as a page or an ordinal, not as a business total.
    assert rejection_reason("正在读第 12 页。", facts) is None


def test_gate_allows_a_number_carried_in_the_facts() -> None:
    facts = _facts("时间范围：2026-07-01 至 2026-07-31。")

    assert rejection_reason("正在取回 2026-07-01 起的数据。", facts) is None
    assert rejection_reason("正在取回 2025-01-01 起的数据。", facts) == "unsupported_number"


def test_gate_rejects_an_empty_frame() -> None:
    assert rejection_reason("   ", _facts("正在取数。")) == "empty"


def test_widening_keeps_first_seen_order_and_ignores_repeats() -> None:
    facts = _facts("第一句。")

    assert facts.widened("第二句。").lines == ("第一句。", "第二句。")
    # Idempotent: a sentence already allowed does not grow the prompt.
    assert facts.widened("第一句。") is facts


def test_merging_a_later_step_never_drops_the_earlier_steps_facts() -> None:
    """The gate re-checks the whole transcript, so facts must accumulate.

    A step that replaced the allowlist with its own would start rejecting
    sentences that were already true when they were written — and a rejected
    frame abandons the source, so a stale allowlist degrades into silence.
    """
    parse = ThinkingFacts(stage="解析", lines=("共 2026 行历史。",))
    fetch = ThinkingFacts(stage="取数", lines=("正在查看本人产量。",))

    merged = parse.merged_with(fetch)

    assert merged.stage == "取数"
    assert merged.lines == ("共 2026 行历史。", "正在查看本人产量。")
    assert rejection_reason("仍按 2026 行统计。", merged) is None


# --------------------------------------------------------------------------- #
# engine: the coalescer
# --------------------------------------------------------------------------- #


def test_coalescer_holds_deltas_until_the_coalescing_threshold() -> None:
    coalescer = ThinkingCoalescer()
    coalescer.feed("正在" * 10)

    assert coalescer.take(_facts("正在取数。")) == ()
    # The forced sweep is what the terminal path uses, so nothing is lost.
    assert coalescer.take(_facts("正在取数。"), force=True) != ()


def test_coalescer_emits_a_frame_once_the_threshold_is_reached() -> None:
    coalescer = ThinkingCoalescer()
    coalescer.feed("正" * (COALESCE_CHARS + 1))

    frames = coalescer.take(_facts("正在取数。"))

    assert len(frames) == 1
    assert coalescer.text == frames[0]


def test_coalescer_holds_back_a_token_a_frame_boundary_would_cut() -> None:
    """Two individually clean frames must not reassemble into a blocked token."""
    coalescer = ThinkingCoalescer(coalesce_chars=10)
    # Padding puts the frame boundary in the middle of the identifier.
    coalescer.feed("。" * 20 + "piecework_records")

    frames = coalescer.take(_facts("正在取数。"))

    assert frames == ("。" * 20,)
    assert coalescer.stopped is False
    # Held back, then refused whole on the next sweep rather than rewritten.
    assert coalescer.take(_facts("正在取数。"), force=True) == ()
    assert coalescer.stopped is True


def test_coalescer_stops_the_source_after_a_rejected_frame() -> None:
    coalescer = ThinkingCoalescer()
    coalescer.feed("正在读 piecework_records 表。")

    assert coalescer.take(_facts("正在取数。"), force=True) == ()
    assert coalescer.stopped is True
    # Once abandoned, the source can never contribute again this round.
    coalescer.feed("正常的话。")
    assert coalescer.take(_facts("正在取数。"), force=True) == ()


def test_coalescer_caps_the_transcript_and_flags_truncation() -> None:
    coalescer = ThinkingCoalescer(max_total_chars=100, coalesce_chars=10)

    for index in range(20):
        # Distinct characters per chunk: identical chunks would be deduplicated
        # as repeats and never spend the budget at all.
        coalescer.feed("".join(chr(0x4E00 + index * 20 + offset) for offset in range(20)))
        coalescer.take(_facts("正在取数。"))
        if coalescer.truncated:
            break

    assert coalescer.truncated is True
    assert len(coalescer.text) <= 100


def test_commit_sends_a_whole_sentence_without_waiting_for_the_threshold() -> None:
    coalescer = ThinkingCoalescer()

    frames = coalescer.commit(_facts("正在取数。"), transcript_line("正在查看本人产量。"))

    # The first row drops the leading newline (契约 §3.3); later rows keep it.
    assert frames == ("正在查看本人产量。",)
    frames = coalescer.commit(_facts("正在取数。"), transcript_line("正在汇总统计。"))
    assert frames == ("\n正在汇总统计。",)
    assert coalescer.text == "正在查看本人产量。\n正在汇总统计。"


def test_only_the_first_row_lacks_a_leading_newline() -> None:
    """契约 §3.3: every transcript row but the first opens on a new line."""
    coalescer = ThinkingCoalescer()
    transcript = ThinkingTranscript(facts=_facts("正在取数。"), coalescer=coalescer)

    for sentence in ("第一句。", "第二句。", "第三句。"):
        coalescer.commit(transcript.facts, transcript_line(sentence))

    assert transcript.text == "第一句。\n第二句。\n第三句。"


def test_a_repeated_row_is_sent_once() -> None:
    coalescer = ThinkingCoalescer()

    first = coalescer.commit(_facts("正在取数。"), transcript_line("正在取数。"))
    second = coalescer.commit(_facts("正在取数。"), transcript_line("正在取数。"))

    assert first != ()
    assert second == ()


def test_commit_still_gates_a_caller_authored_sentence() -> None:
    """Being written here rather than by the model is not a reason to trust it."""
    coalescer = ThinkingCoalescer()

    frames = coalescer.commit(_facts("正在取数。"), transcript_line("共 4321 行。"))

    assert frames == ()
    assert coalescer.stopped is True


def test_a_frame_never_exceeds_the_contract_frame_limit() -> None:
    coalescer = ThinkingCoalescer(coalesce_chars=10)

    frames = coalescer.commit(_facts("正在取数。"), transcript_line("正" * 500))

    assert frames != ()
    assert all(len(frame) <= MAX_FRAME_CHARS for frame in frames)
    assert "".join(frames).strip() == "正" * 500


# --------------------------------------------------------------------------- #
# engine: the pager's fact sentences
# --------------------------------------------------------------------------- #


def test_fetch_progress_watch_reports_each_mark_once() -> None:
    watch = FetchProgressWatch()

    assert watch.fresh_sentence() is None
    watch.observe(MesFetchProgress(page=1, rows=2_000, total=30_000))
    first = watch.fresh_sentence()
    assert first is not None and "2,000" in first and "30,000" in first and "第 1 页" in first
    # The same mark is not news; a later page is.
    assert watch.fresh_sentence() is None
    watch.observe(MesFetchProgress(page=2, rows=5_000, total=30_000))
    second = watch.fresh_sentence()
    assert second is not None and "5,000" in second
    assert watch.observations == 2


def test_fetch_progress_watch_omits_a_total_the_pager_does_not_know() -> None:
    watch = FetchProgressWatch()
    watch.observe(MesFetchProgress(page=3, rows=600))

    sentence = watch.fresh_sentence()

    assert sentence is not None
    assert "共" not in sentence
    assert "600" in sentence


class _SteppingClock:
    """Monotonic seconds a test advances by hand."""

    def __init__(self) -> None:
        self._now = 0.0

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


def test_fetch_progress_watch_reports_the_elapsed_wait_when_no_page_lands() -> None:
    clock = _SteppingClock()
    watch = FetchProgressWatch(wait_seconds=5.0, monotonic=clock)

    # A fetch that never pages publishes no counters, so counters alone would
    # leave the longest window of the run completely silent.
    clock.advance(4.0)
    assert watch.fresh_sentence() is None

    clock.advance(1.0)
    first = watch.fresh_sentence()
    assert first is not None and "已等待 5 秒" in first

    # The total, not the interval: a repeating "已等待 5 秒" would both
    # understate the wait and be dropped downstream as a repeat.
    clock.advance(5.0)
    second = watch.fresh_sentence()
    assert second is not None and "已等待 10 秒" in second


def test_a_landed_page_restarts_the_wait() -> None:
    clock = _SteppingClock()
    watch = FetchProgressWatch(wait_seconds=5.0, monotonic=clock)

    clock.advance(3.0)
    watch.observe(MesFetchProgress(page=1, rows=100))
    assert watch.fresh_sentence() is not None

    # A landed page is the newest thing there is to say, so the wait restarts
    # from it rather than reporting on top of it.
    clock.advance(4.0)
    assert watch.fresh_sentence() is None

    clock.advance(1.0)
    sentence = watch.fresh_sentence()
    assert sentence is not None and "已等待 8 秒" in sentence


def test_the_wait_sentence_stays_off_until_it_is_asked_for() -> None:
    clock = _SteppingClock()
    watch = FetchProgressWatch(monotonic=clock)

    clock.advance(600.0)

    assert watch.fresh_sentence() is None


# --------------------------------------------------------------------------- #
# wiring: a round that narrates
# --------------------------------------------------------------------------- #


@dataclass
class _SlowRunner(RecordingCapabilityRunner):
    """Capability runner whose fetch takes long enough to narrate over."""

    delay_seconds: float = 0.0

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return await super().run(request)


@dataclass
class _StreamingGateway:
    """Narration gateway double: scripted deltas, one script per call.

    ``delay_seconds`` is what makes interleaving observable at all: frames are
    only flushed from *inside* a wait, so an instantaneous stream would arrive
    in the single closing sweep and the test could not tell interleaving apart
    from buffering.
    """

    scripts: list[list[str]] = field(default_factory=lambda: [])
    failures: list[Exception | None] = field(default_factory=lambda: [])
    requests: list[ModelRequest] = field(default_factory=lambda: [])
    delay_seconds: float = 0.0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index < len(self.failures) and self.failures[index] is not None:
            raise self.failures[index]  # pyright: ignore[reportGeneralTypeIssues]
        script = self.scripts[min(index, len(self.scripts) - 1)] if self.scripts else []
        for piece in script:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield ModelDelta(text=piece)


def _credential() -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))


def _authorization() -> AuthorizationService:
    return AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={
                ("tenant-a", "user-a"): membership("user-a", "tenant-a", "emp-1", Role.EMPLOYEE)
            }
        ),
        organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
        versions=FixedScopeVersionAssigner(),
    )


async def _no_sleep(_: float) -> None:
    return None


def build(
    *,
    stream_gateway: _StreamingGateway | None = None,
    limits: SessionLimits | None = None,
    runner: RecordingCapabilityRunner | None = None,
    store: InMemoryInteractionStore | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[SessionService, InMemoryInteractionStore]:
    parser = CapabilityIntentParser(
        ScriptedModelGateway(contents=[INTENT_PAYLOAD]),
        CATALOG,
        model_alias="factory-fast",
        timezone_name="Asia/Shanghai",
    )
    resolved_store = store or InMemoryInteractionStore()
    service = SessionService(
        resolved_store,
        _authorization(),
        parser,
        runner or RecordingCapabilityRunner(),
        FrozenClock(NOW),
        new_id=SequentialIds(),
        limits=limits,
        sleep=sleep or _no_sleep,
        business_filters=BusinessFilterResolver(_EmptyDirectory()),
        stream_gateway=stream_gateway,
    )
    return service, resolved_store


async def _start(service: SessionService) -> InteractionId:
    record = await service.start(_credential(), StartRequest(session_id=SESSION, text="上个月产量"))
    return record.interaction_id


async def drain(service: SessionService, interaction_id: InteractionId) -> list[SessionEvent]:
    stream = service.stream(_credential(), interaction_id)
    return [event async for event in stream]


def _thinking(events: list[SessionEvent]) -> list[SessionEvent]:
    return [event for event in events if event.name == INTERACTION_THINKING]


def _transcript(events: list[SessionEvent]) -> str:
    return "".join(str(frame.data["text"]) for frame in _thinking(events))


def _result(events: list[SessionEvent]) -> SessionEvent:
    matches = [event for event in events if event.name == INTERACTION_RESULT]
    assert len(matches) == 1
    return matches[0]


async def _run_a_round(
    *, stream_gateway: _StreamingGateway | None = None
) -> tuple[list[SessionEvent], InMemoryInteractionStore]:
    service, store = build(stream_gateway=stream_gateway)
    return await drain(service, await _start(service)), store


@pytest.mark.asyncio
async def test_frames_are_flat_single_line_json_numbered_from_one() -> None:
    """契约 §3.2 + §7-1: a flat ``data`` object whose JSON holds no raw newline."""
    events, _ = await _run_a_round()

    frames = _thinking(events)
    assert frames

    for frame in frames:
        # ``duration_ms`` rides along on every stage event; the rest are the
        # transcript's own fields.
        assert {"text", "seq", "done", "elapsed_ms"} <= set(frame.data)
        assert set(frame.data) <= {
            "text",
            "seq",
            "done",
            "elapsed_ms",
            "truncated",
            "duration_ms",
        }
        assert "payload" not in frame.data
        # The contract's reader parses line by line: a real newline inside the
        # text must survive only as an escape.
        encoded = json.dumps(frame.data, ensure_ascii=False)
        assert "\n" not in encoded
        assert json.loads(encoded) == frame.data
        elapsed = frame.data["elapsed_ms"]
        assert isinstance(elapsed, int) and elapsed >= 0

    assert [frame.data["seq"] for frame in frames] == list(range(1, len(frames) + 1))


@pytest.mark.asyncio
async def test_every_frame_but_the_last_carries_text_and_the_last_marks_done() -> None:
    """契约 §3.2: only three ``text``/``done`` combinations are legal."""
    events, _ = await _run_a_round()

    frames = _thinking(events)

    for frame in frames[:-1]:
        assert str(frame.data["text"]).strip()
        assert frame.data.get("done", False) is False
    assert frames[-1].data["done"] is True
    assert frames[-1].data["text"] == ""


@pytest.mark.asyncio
async def test_the_whole_transcript_lands_before_the_result() -> None:
    """契约 §7-5: a frame after ``result`` is discarded by the client."""
    events, _ = await _run_a_round()

    frames = _thinking(events)
    result = _result(events)

    assert frames
    assert max(frame.sequence for frame in frames) < result.sequence
    assert events[-1].name == terminal_event_name(InteractionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_a_round_without_a_streaming_gateway_still_narrates_from_facts() -> None:
    """Facts alone are a complete transcript; the model only adds wording."""
    events, _ = await _run_a_round(stream_gateway=None)

    text = _transcript(events)

    assert "正在理解您的问题" in text
    assert "正在核对权限与可查询范围" in text
    assert "正在向工厂系统取数" in text
    assert "正在汇总本次结果并生成答复" in text


@pytest.mark.asyncio
async def test_the_transcript_never_names_an_internal_artifact() -> None:
    """契约 §7-9: the transcript is shown to every role, so it must be clean."""
    events, _ = await _run_a_round()

    text = _transcript(events)

    assert "FR-001" not in text
    assert "fr001" not in text
    assert "_" not in text
    assert "select" not in text.lower()


@pytest.mark.asyncio
async def test_the_transcript_is_persisted_once_as_one_thinking_message() -> None:
    """契约 §4: a refreshed client renders one collapsed block per round."""
    events, store = await _run_a_round()

    frames = _thinking(events)
    messages = [message for message in store.messages if message.kind is MessageKind.THINKING]

    assert len(messages) == 1
    assert messages[0].text == "".join(str(frame.data["text"]) for frame in frames)
    assert messages[0].payload == {"elapsed_ms": frames[-1].data["elapsed_ms"]}


@pytest.mark.asyncio
async def test_narration_disabled_emits_no_frame_and_persists_no_message() -> None:
    service, store = build(limits=SessionLimits(thinking_enabled=False))

    events = await drain(service, await _start(service))

    assert _thinking(events) == []
    assert [message for message in store.messages if message.kind is MessageKind.THINKING] == []


@pytest.mark.asyncio
async def test_the_fragment_counter_restarts_at_one_every_round() -> None:
    """契约 §8-13: each round is an independent block numbered from 1."""
    service, _ = build()

    first = await drain(service, await _start(service))
    second = await drain(service, await _start(service))

    assert _thinking(first)[0].data["seq"] == 1
    assert _thinking(second)[0].data["seq"] == 1
    assert len(_thinking(second)) == len(_thinking(first))


@pytest.mark.asyncio
async def test_a_failed_narration_call_leaves_the_answer_intact() -> None:
    """The transcript is an aid; it must never fail the round it describes."""
    gateway = _StreamingGateway(
        failures=[ModelGatewayError(ModelErrorCategory.UNAVAILABLE, "gateway request failed")]
    )

    events, _ = await _run_a_round(stream_gateway=gateway)

    assert _result(events) is not None
    # The deterministic facts survive the dead narration call.
    assert _thinking(events)


@pytest.mark.asyncio
async def test_a_failing_stream_still_meters_the_narration_attempt() -> None:
    """A failed narration is still spend, so it must be recorded either way."""
    broken = ModelGatewayError(ModelErrorCategory.PROTOCOL, "stream broke")
    # One entry per narration call (the intent wait and the fetch wait).
    gateway = _StreamingGateway(failures=[broken, broken])
    service, store = build(stream_gateway=gateway)

    await drain(service, await _start(service))

    spend = [
        event
        for event in store.usage_events
        if event.payload.get("stage") == ModelStage.THINKING.value
    ]

    assert spend
    assert {event.payload.get("status") for event in spend} == {"failed"}


@pytest.mark.asyncio
async def test_the_narration_call_is_shaped_as_a_bounded_thinking_stage() -> None:
    gateway = _StreamingGateway()
    service, _ = build(
        stream_gateway=gateway,
        limits=SessionLimits(thinking_max_output_tokens=64, thinking_timeout_seconds=3.0),
    )

    await drain(service, await _start(service))

    assert gateway.requests
    request = gateway.requests[0]
    assert request.stage is ModelStage.THINKING
    assert request.model_alias == "factory-fast"
    assert request.max_output_tokens == 64
    assert request.timeout_seconds == 3.0
    # The narration prompt is written for the model but disclosed verbatim to
    # the user, so it carries facts and never business rows.
    assert request.messages[0].role == "system"
    assert "事实" in request.messages[1].content


#: The marker identifies the model's own prose in the transcript. 128 characters
#: is past ``COALESCE_CHARS``, so the tick loop releases it as a frame *while*
#: the fetch is still running rather than only in the closing sweep.
_FETCH_PROSE = "正在向工厂系统逐页取回所需记录，"
_MODEL_SCRIPTS = [[_FETCH_PROSE * 8, "工厂系统仍在返回剩余部分，稍后开始汇总。"]]


@pytest.mark.asyncio
async def test_streamed_prose_is_flushed_from_inside_the_wait_it_describes() -> None:
    """Interleaving: the model's wording arrives before the step it narrates ends.

    The total prose is 149 characters, which fits in one frame (the limit is
    200). A closing sweep can therefore only ever emit a single frame, so two
    frames carrying the marker prove that one was released by a tick-loop flush
    — that is, before the fetch returned.
    """
    gateway = _StreamingGateway(scripts=_MODEL_SCRIPTS, delay_seconds=0.002)
    service, _ = build(
        stream_gateway=gateway,
        limits=SessionLimits(
            thinking_tick_seconds=0.002,
            # The follower charges one heartbeat interval of its follow budget
            # per quiet pass, so an immediate no-op sleep would burn the whole
            # budget in microseconds. A real (tiny) interval keeps the budget
            # tracking wall-clock time, which is what it models.
            heartbeat_seconds=0.001,
        ),
        runner=_SlowRunner(delay_seconds=0.06),
        sleep=asyncio.sleep,
    )

    events = await drain(service, await _start(service))

    frames = _thinking(events)
    composed_at = next(
        frame.sequence
        for frame in frames
        if "正在汇总本次结果并生成答复" in str(frame.data["text"])
    )
    model_frames = [frame for frame in frames if "工厂系统" in str(frame.data["text"])]

    assert len(model_frames) >= 2
    assert max(frame.sequence for frame in model_frames) < composed_at
    # Nothing was dropped on the way: the whole scripted script is on screen.
    assert "".join(_MODEL_SCRIPTS[0]) in _transcript(events)


@pytest.mark.asyncio
async def test_narration_is_additive_to_the_rounds_own_events() -> None:
    """The switch removes the extra call, never an event of the answer itself."""
    on_service, _ = build(limits=SessionLimits(thinking_enabled=True))
    on_events = await drain(on_service, await _start(on_service))
    off_service, _ = build(limits=SessionLimits(thinking_enabled=False))
    off_events = await drain(off_service, await _start(off_service))

    assert not _thinking(off_events)
    assert [event.name for event in off_events] == [
        event.name for event in on_events if event.name != INTERACTION_THINKING
    ]


@pytest.mark.asyncio
async def test_a_fetch_that_never_pages_still_narrates_its_wait() -> None:
    """The counters say nothing when nothing pages; the wait sentence covers it.

    One large request answered in a single round trip is the slowest and
    quietest window of a run: it publishes no page mark at all, so before the
    wait sentence existed it produced no frames whatever between the fetch's
    opening facts and its result.
    """
    service, _ = build(
        stream_gateway=_StreamingGateway(),
        limits=SessionLimits(thinking_wait_seconds=1.0, thinking_tick_seconds=0.05),
        runner=_SlowRunner(delay_seconds=1.3),
        sleep=asyncio.sleep,
    )

    events = await drain(service, await _start(service))

    assert "正在等待工厂系统返回数据" in _transcript(events)
