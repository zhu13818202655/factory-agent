"""Trace read model: the timeline contract a view renders without guessing.

Three properties are load-bearing and are asserted below rather than left to
inspection:

* the axis never overflows (every span fits inside ``total_ms``);
* ``content_capture`` alone decides whether a ``payload`` section exists — never
  the accidental presence of stored data;
* ``truncated`` reaches the consumer unchanged, because a shortened payload
  read as a complete one is how a developer reaches the wrong conclusion.
"""

from datetime import datetime, timedelta, timezone

import pytest

from factory_agent.application.trace import (
    TraceCapability,
    TraceService,
    assemble_trace,
    duration_breakdown,
)
from factory_agent.domain import (
    CapabilityId,
    InteractionId,
    InteractionRecord,
    InteractionStatus,
    SessionId,
    SessionState,
    TenantId,
    UserId,
)
from factory_agent.ports.session import InteractionOwner
from factory_agent.ports.trace import (
    InteractionFactRecord,
    LlmSpanRecord,
    MesSpanRecord,
    PhaseRecord,
    TraceDebugPayload,
    TraceFacts,
)

NOW = datetime(2026, 9, 22, 0, 20, 31, tzinfo=timezone.utc)
OWNER = InteractionOwner(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))

FULL = TraceCapability(
    environment="dev", report_available=True, reason=None, content_capture="full"
)
NONE = TraceCapability(
    environment="prod", report_available=False, reason="environment", content_capture="none"
)


def record(
    *,
    created_at: datetime = NOW,
    completed_at: datetime | None = None,
    status: InteractionStatus = InteractionStatus.COMPLETED,
) -> InteractionRecord:
    end = completed_at or (created_at + timedelta(milliseconds=4_008))
    return InteractionRecord(
        interaction_id=InteractionId("int-1"),
        session_id=SessionId("session-1"),
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        status=status,
        state=SessionState.ANSWERED,
        input_text="上个月计件工资是多少",
        capability_id=CapabilityId("FR-003"),
        clarification_rounds=0,
        last_event_sequence=9,
        error_category=None,
        created_at=created_at,
        updated_at=end,
        completed_at=completed_at or end,
    )


def phase(name: str, offset_ms: int, status: str = "ok") -> PhaseRecord:
    return PhaseRecord(
        name=name,
        label=name,
        status=status,
        sequence=offset_ms,
        occurred_at=NOW + timedelta(milliseconds=offset_ms),
    )


def mes_span(
    operation_id: str = "SystemToken",
    *,
    started_ms: int = 0,
    duration_ms: int = 12,
    status: str = "completed",
) -> MesSpanRecord:
    started = NOW + timedelta(milliseconds=started_ms)
    return MesSpanRecord(
        operation_id=operation_id,
        page_count=1,
        row_count_bucket="1-10",
        duration_ms=duration_ms,
        status=status,
        error_category=None,
        started_at=started,
        ended_at=started + timedelta(milliseconds=duration_ms),
        occurred_at=started + timedelta(milliseconds=duration_ms),
    )


def llm_span(
    logical_call_id: str = "lc_8f21",
    *,
    started_ms: int = 51,
    duration_ms: int = 1_180,
    status: str = "completed",
    attempt: int = 1,
) -> LlmSpanRecord:
    started = NOW + timedelta(milliseconds=started_ms)
    return LlmSpanRecord(
        logical_call_id=logical_call_id,
        stage="extract",
        model_alias="factory-fast",
        actual_model="Qwen3-32B-Instruct",
        attempt=attempt,
        prompt_tokens=1_842,
        completion_tokens=96,
        cached_tokens=0,
        reasoning_tokens=0,
        duration_ms=duration_ms,
        status=status,
        fallback_reason=None,
        error_category=None,
        started_at=started,
        ended_at=started + timedelta(milliseconds=duration_ms),
        occurred_at=started + timedelta(milliseconds=duration_ms),
    )


def test_phases_collapse_repeats_and_partition_the_timeline() -> None:
    """A stage summary, not a log dump: one row per stage, windows contiguous."""
    facts = TraceFacts(
        phases=(
            phase("parsing", 51),
            # ``interaction.progress`` shares its state with the transition that
            # opened the stage, so it must collapse rather than duplicate.
            phase("parsing", 300, status="running"),
            phase("authorizing", 1_231),
            phase("executing", 1_872),
            phase("answered", 4_008),
        ),
        interaction_fact=InteractionFactRecord(
            status="completed",
            duration_ms=4_008,
            mes_duration_ms=1_872,
            llm_duration_ms=1_180,
            local_duration_ms=956,
            result_rows_bucket="1-10",
        ),
    )

    document = assemble_trace(record(), facts, capability=FULL)
    phases = document["phases"]

    assert [item["name"] for item in phases] == [
        "parsing",
        "authorizing",
        "executing",
        "answered",
    ]
    assert [item["offset_ms"] for item in phases] == [51, 1_231, 1_872, 4_008]
    assert [item["id"] for item in phases] == [
        "ph_parsing",
        "ph_authorizing",
        "ph_executing",
        "ph_answered",
    ]
    # Windows tile the axis: each ends where the next begins.
    assert [item["duration_ms"] for item in phases] == [1_180, 641, 2_136, 0]
    assert phases[-1]["terminal"] is True


def test_span_offsets_and_durations_fit_inside_the_axis() -> None:
    """The plan's verification criterion: no gap, no overflow."""
    facts = TraceFacts(
        mes_spans=(mes_span("SystemToken"), mes_span("YskQuery", started_ms=100, duration_ms=900)),
        llm_spans=(llm_span(),),
        interaction_fact=InteractionFactRecord(
            status="completed",
            duration_ms=4_008,
            mes_duration_ms=912,
            llm_duration_ms=1_180,
            local_duration_ms=1_916,
        ),
    )

    document = assemble_trace(record(), facts, capability=FULL)

    for span in document["spans"]:
        assert span["offset_ms"] >= 0
        assert span["offset_ms"] + span["duration_ms"] <= document["total_ms"]


def test_a_span_that_outlives_the_ledger_extends_the_axis() -> None:
    """Otherwise every bar after it would be drawn in the wrong place."""
    facts = TraceFacts(
        mes_spans=(mes_span("YskQuery", started_ms=1_000, duration_ms=9_000),),
        interaction_fact=InteractionFactRecord(
            status="completed",
            duration_ms=2_000,
            mes_duration_ms=9_000,
            llm_duration_ms=0,
            local_duration_ms=0,
        ),
    )

    document = assemble_trace(record(), facts, capability=FULL)

    assert document["total_ms"] == 10_000
    assert document["ledger"]["total_ms"] == 2_000


def test_llm_spans_report_the_retry_grouping_fields() -> None:
    facts = TraceFacts(
        llm_spans=(
            llm_span(attempt=1, status="failed"),
            llm_span(attempt=2, started_ms=1_300, status="completed"),
        )
    )

    document = assemble_trace(record(), facts, capability=FULL)
    spans = document["spans"]

    assert [item["span_id"] for item in spans] == ["llm:lc_8f21", "llm:lc_8f21"]
    assert [item["attempt"] for item in spans] == [1, 2]
    assert [item["status"] for item in spans] == ["error", "ok"]
    assert spans[0]["tokens"] == {
        "prompt": 1_842,
        "completion": 96,
        "cached": 0,
        "reasoning": 0,
    }


def test_content_capture_none_removes_the_payload_section_entirely() -> None:
    """Shape follows the declaration, not the contents of the store.

    Data is deliberately present in the facts here: with capture off it must be
    unreachable, so a stale row cannot widen the response.
    """
    facts = TraceFacts(
        mes_spans=(mes_span(),),
        debug_payloads={
            "mes:SystemToken:2026-09-22T00:20:31+00:00": TraceDebugPayload(
                span_key="mes:SystemToken:2026-09-22T00:20:31+00:00",
                input={"dh": "SO-1"},
                output={"rows": 1},
                truncated=False,
            )
        },
    )

    document = assemble_trace(record(), facts, capability=NONE)

    assert document["content_capture"] == "none"
    assert document["spans"][0]["payload"] is None
    assert document["report"] == {
        "available": False,
        "reason": "environment",
        "url": "/v1/interactions/int-1/trace.html",
        "content_capture": "none",
    }


def test_content_capture_full_gives_every_span_a_payload_section() -> None:
    """One declaration for the consumer, including for spans with no capture.

    ``available: false`` is an answer; a missing key would be a guess.
    """
    key = "mes:SystemToken:2026-09-22T00:20:31+00:00"
    facts = TraceFacts(
        mes_spans=(mes_span("SystemToken"), mes_span("YskQuery", started_ms=100)),
        debug_payloads={
            key: TraceDebugPayload(
                span_key=key,
                input={"dh": "SO-1"},
                output={"rows": 250_000},
                truncated=True,
                original_rows=250_000,
                original_bytes=99_000_000,
            )
        },
    )

    document = assemble_trace(record(), facts, capability=FULL)
    captured, missing = document["spans"]

    assert captured["payload"] == {
        "available": True,
        "input": {"dh": "SO-1"},
        "output": {"rows": 250_000},
        "truncated": True,
        "original_rows": 250_000,
        "original_bytes": 99_000_000,
    }
    assert captured["payload"]["truncated"] is True
    assert missing["payload"] == {
        "available": False,
        "input": None,
        "output": None,
        "truncated": False,
        "original_rows": None,
        "original_bytes": None,
    }


def test_a_report_that_is_merely_disabled_says_so() -> None:
    """``disabled`` and ``environment`` are different answers for an operator."""
    capability = TraceCapability(
        environment="local", report_available=False, reason="disabled", content_capture="none"
    )

    document = assemble_trace(record(), TraceFacts(), capability=capability)

    assert document["report"]["available"] is False
    assert document["report"]["reason"] == "disabled"


def test_the_document_declares_its_schema_and_identity() -> None:
    document = assemble_trace(record(), TraceFacts(), capability=FULL, request_id="req-9f3a")

    assert document["schema_version"] == "1.0"
    assert document["interaction_id"] == "int-1"
    assert document["request_id"] == "req-9f3a"
    assert document["session_id"] == "session-1"
    assert document["capability_id"] == "FR-003"
    assert document["status"] == "completed"
    assert document["started_at"] == NOW.isoformat()
    assert document["phases"] == []
    assert document["spans"] == []


def test_duration_breakdown_reads_the_three_segment_ledger() -> None:
    facts = TraceFacts(
        interaction_fact=InteractionFactRecord(
            status="completed",
            duration_ms=4_008,
            mes_duration_ms=None,
            llm_duration_ms=1_180,
            local_duration_ms=956,
        )
    )

    assert duration_breakdown(facts) == {
        "total_ms": 4_008,
        "mes_ms": 0,
        "llm_ms": 1_180,
        "local_ms": 956,
    }
    assert duration_breakdown(TraceFacts()) == {
        "total_ms": 0,
        "mes_ms": 0,
        "llm_ms": 0,
        "local_ms": 0,
    }


def test_duration_breakdown_sums_the_spans_the_waterfall_draws() -> None:
    """Header and waterfall must never disagree.

    The interaction fact carries the capability-run wall clock, which absorbs
    everything between calls (row validation, sandbox inserts, queueing). The
    header instead reports what the calls themselves cost, so "MES 取数" can
    never dwarf the MES bars drawn right below it; the remainder is 本地其他.
    """
    facts = TraceFacts(
        mes_spans=(
            mes_span(started_ms=1_000, duration_ms=27_236),
            mes_span(started_ms=30_000, duration_ms=26_921),
            mes_span(started_ms=169_000, duration_ms=422),
        ),
        llm_spans=(
            llm_span(started_ms=0, duration_ms=1_025),
            llm_span(started_ms=1_300, duration_ms=831),
        ),
        interaction_fact=InteractionFactRecord(
            status="completed",
            duration_ms=171_160,
            mes_duration_ms=168_487,
            llm_duration_ms=3_364,
            local_duration_ms=0,
        ),
    )

    assert duration_breakdown(facts) == {
        "total_ms": 171_160,
        # Span sums: 27,236 + 26,921 + 422 and 1,025 + 831 — the bars as drawn.
        "mes_ms": 54_579,
        "llm_ms": 1_856,
        # Residual wall clock: 171,160 − 54,579 − 1,856.
        "local_ms": 114_725,
    }


class _Store:
    """Ownership-filtered interaction lookup, as the real store behaves."""

    def __init__(self, found: InteractionRecord | None) -> None:
        self._found = found
        self.seen: list[InteractionOwner] = []

    async def get_interaction(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord | None:
        self.seen.append(owner)
        return self._found


class _Facts:
    def __init__(self, facts: TraceFacts) -> None:
        self._facts = facts
        self.seen: list[dict[str, str]] = []

    async def load(self, *, tenant_id: str, user_id: str, interaction_id: str) -> TraceFacts:
        self.seen.append(
            {"tenant_id": tenant_id, "user_id": user_id, "interaction_id": interaction_id}
        )
        return self._facts


@pytest.mark.asyncio
async def test_the_service_returns_none_for_an_interaction_the_caller_does_not_own() -> None:
    """Missing and foreign are the same answer, and no fact is read either way."""
    store = _Store(None)
    facts = _Facts(TraceFacts())
    service = TraceService(store, facts, FULL)  # type: ignore[arg-type]

    assert await service.build(OWNER, InteractionId("int-1")) is None
    assert facts.seen == []


@pytest.mark.asyncio
async def test_the_service_scopes_the_fact_read_to_the_owner_pair() -> None:
    store = _Store(record())
    facts = _Facts(TraceFacts())
    service = TraceService(store, facts, FULL)  # type: ignore[arg-type]

    document = await service.build(OWNER, InteractionId("int-1"))

    assert document is not None
    # Facts carry only tenant_id, so the ownership decision must have been made
    # on the interaction row first — and the pair travels with the read.
    assert facts.seen == [{"tenant_id": "tenant-a", "user_id": "user-a", "interaction_id": "int-1"}]
