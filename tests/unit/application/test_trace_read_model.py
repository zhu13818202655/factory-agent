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
from typing import Mapping

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


def local_payload(
    key: str = "cache:c1",
    *,
    kind: str = "cache",
    operation: str = "lookup",
    offset_ms: int = 100,
    duration_ms: int = 20,
    role: str = "leaf",
    status: str = "ok",
    details: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> TraceDebugPayload:
    timing: dict[str, object] = {
        "started_at": (NOW + timedelta(milliseconds=offset_ms)).isoformat(),
        "duration_ms": duration_ms,
        "status": status,
        "reason": None,
        "error_category": None,
        "timing_role": role,
        "parent_span_id": None,
    }
    timing.update(metadata or {})
    return TraceDebugPayload(
        span_key=key,
        input={"sql": "SELECT count(*) FROM authorized_data"} if kind == "duckdb" else {},
        output={"_trace": timing, "details": dict(details or {})},
        truncated=False,
        kind=kind,
        operation_id=operation,
        stage=operation,
        occurred_at=NOW + timedelta(milliseconds=offset_ms + duration_ms),
    )


def test_local_spans_join_the_timeline_and_preserve_the_legacy_ledger() -> None:
    cache = local_payload(details={"input_rows": 30, "output_rows": 7})
    query = local_payload("duckdb:q1", kind="duckdb", operation="query", offset_ms=150)
    facts = TraceFacts(
        mes_spans=(mes_span(started_ms=200),),
        llm_spans=(llm_span(started_ms=0, duration_ms=40),),
        debug_payloads={query.span_key: query, cache.span_key: cache},
        interaction_fact=InteractionFactRecord("completed", 4_008, 12, 40, 3_956),
    )

    document = assemble_trace(record(), facts, capability=FULL)

    assert [span["kind"] for span in document["spans"]] == ["llm", "cache", "duckdb", "mes"]
    cached = document["spans"][1]
    assert cached["duration_ms"] == 20
    assert cached["offset_ms"] == 100
    assert cached["operation_id"] == "lookup"
    assert cached["details"] == {"input_rows": 30, "output_rows": 7}
    assert cached["payload"]["available"] is True
    assert document["ledger"] == duration_breakdown(facts)
    assert document["processing_summary"]["complete"] is False


def test_local_spans_are_hidden_when_capture_is_not_permitted() -> None:
    cached = local_payload()

    document = assemble_trace(
        record(), TraceFacts(debug_payloads={cached.span_key: cached}), capability=NONE
    )

    assert document["spans"] == []
    summary = document["processing_summary"]
    assert summary["state"] == "unavailable"
    assert summary["reason"] == "content_capture_disabled"
    assert summary["kinds"]["cache"]["span_count"] is None
    assert all(value is None for value in summary["cache_metrics"].values())


@pytest.mark.parametrize(
    "metadata",
    [
        {"duration_ms": -1},
        {"duration_ms": True},
        {"duration_ms": "20"},
        {"duration_ms": 1.2},
        {"started_at": "not-an-instant"},
        {"started_at": "2026-09-22T00:20:31"},
        {"status": "unknown"},
        {"timing_role": "unknown"},
        {"parent_span_id": {"bad": "shape"}},
    ],
)
def test_invalid_local_metadata_does_not_become_a_timed_span(
    metadata: Mapping[str, object],
) -> None:
    cached = local_payload(metadata=metadata)

    document = assemble_trace(
        record(), TraceFacts(debug_payloads={cached.span_key: cached}), capability=FULL
    )

    assert document["spans"] == []
    assert document["processing_summary"]["reason"] == "not_captured_or_expired"


def test_old_payload_rows_and_unknown_kinds_do_not_invent_local_spans() -> None:
    old = TraceDebugPayload("cache:old", {"a": 1}, {"b": 2}, False)
    unknown = local_payload("other:x", kind="unknown")

    document = assemble_trace(
        record(),
        TraceFacts(debug_payloads={old.span_key: old, unknown.span_key: unknown}),
        capability=FULL,
    )

    assert document["spans"] == []
    assert document["processing_summary"]["kinds"]["duckdb"]["cumulative_ms"] is None


def test_a_local_span_can_extend_the_axis_without_rewriting_the_ledger() -> None:
    query = local_payload("duckdb:q1", kind="duckdb", offset_ms=3_900, duration_ms=600)
    document = assemble_trace(
        record(),
        TraceFacts(
            debug_payloads={query.span_key: query},
            interaction_fact=InteractionFactRecord("completed", 4_008, 0, 0, 4_008),
        ),
        capability=FULL,
    )

    assert document["total_ms"] == 4_500
    assert document["ledger"]["total_ms"] == 4_008
    assert document["processing_summary"]["timing"]["axis_ms"] == 4_500


def test_processing_occupancy_excludes_parents_and_accounts_for_concurrent_categories() -> None:
    entries = (
        local_payload("cache:parent", offset_ms=0, duration_ms=1_000, role="parent"),
        local_payload("cache:first", offset_ms=100, duration_ms=300),
        local_payload("cache:second", offset_ms=200, duration_ms=300),
        local_payload("duckdb:q1", kind="duckdb", offset_ms=300, duration_ms=300),
    )
    document = assemble_trace(
        record(),
        TraceFacts(
            mes_spans=(mes_span(started_ms=0, duration_ms=250),),
            debug_payloads={item.span_key: item for item in entries},
        ),
        capability=FULL,
    )
    summary = document["processing_summary"]
    cache = summary["kinds"]["cache"]
    timing = summary["timing"]

    assert cache["span_count"] == 3
    assert cache["leaf_count"] == 2
    assert cache["parent_count"] == 1
    assert cache["cumulative_ms"] == 600
    assert cache["occupancy_ms"] == 400
    assert cache["exclusive_ms"] == 50
    assert timing["occupancy_ms"] == 600
    assert timing["overlap_ms"] == 350
    assert timing["exclusive_ms"] == {"mes": 100, "llm": 0, "cache": 50, "duckdb": 100}
    assert timing["uncovered_ms"] == 3_408
    assert (
        sum(timing["exclusive_ms"].values()) + timing["overlap_ms"] + timing["uncovered_ms"]
        == timing["axis_ms"]
    )


def test_processing_cache_metrics_distinguish_storage_hit_adoption_and_reuse() -> None:
    entries = (
        local_payload(
            "cache:lookup", details={"metric_counts": {"cache_reads": 1, "storage_hits": 1}}
        ),
        local_payload("cache:invalid", status="skipped", details={"outcome": "invalid"}),
        local_payload(
            "cache:reuse",
            operation="interaction_reuse",
            details={"metric_counts": {"interaction_reuses": 1}},
        ),
    )
    document = assemble_trace(
        record(),
        TraceFacts(debug_payloads={item.span_key: item for item in entries}),
        capability=FULL,
    )
    metrics = document["processing_summary"]["cache_metrics"]

    assert metrics["cache_reads"] == 1
    assert metrics["storage_hits"] == 1
    assert metrics["interaction_reuses"] == 1
    assert metrics["adopted_snapshots"] is None
    assert metrics["realtime_fallbacks"] is None


def test_processing_treats_observed_zero_duration_differently_from_missing_work() -> None:
    query = local_payload("duckdb:empty", kind="duckdb", duration_ms=0, details={"result_rows": 0})
    document = assemble_trace(
        record(), TraceFacts(debug_payloads={query.span_key: query}), capability=FULL
    )
    summary = document["processing_summary"]

    assert summary["kinds"]["duckdb"]["cumulative_ms"] == 0
    assert summary["kinds"]["duckdb"]["leaf_count"] == 1
    assert summary["kinds"]["cache"]["cumulative_ms"] is None
    assert summary["timing"]["occupancy_ms"] == 0
    assert summary["timing"]["uncovered_ms"] == 4_008


def test_local_payload_truncation_is_preserved() -> None:
    query = local_payload("duckdb:q1", kind="duckdb")
    truncated = TraceDebugPayload(
        span_key=query.span_key,
        input={"sql": "SELECT 1 /* truncated */"},
        output=query.output,
        truncated=True,
        original_bytes=999_999,
        kind=query.kind,
        operation_id=query.operation_id,
    )

    document = assemble_trace(
        record(), TraceFacts(debug_payloads={truncated.span_key: truncated}), capability=FULL
    )

    span = document["spans"][0]
    assert span["payload"]["truncated"] is True
    assert span["payload"]["original_bytes"] == 999_999
    assert document["processing_summary"]["state"] == "partial"


def test_query_and_load_counters_use_actual_operation_and_status_records() -> None:
    entries = (
        local_payload("duckdb:q-ok", kind="duckdb", operation="duckdb.query"),
        local_payload("duckdb:q-denied", kind="duckdb", operation="duckdb.query", status="denied"),
        local_payload("duckdb:q-error", kind="duckdb", operation="duckdb.query", status="error"),
        local_payload(
            "duckdb:q-cancel", kind="duckdb", operation="duckdb.query", status="cancelled"
        ),
        local_payload("duckdb:load", kind="duckdb", operation="duckdb.register", role="parent"),
        local_payload("duckdb:replace", kind="duckdb", operation="duckdb.replace", role="parent"),
        local_payload(
            "cache:shadow",
            operation="cache.shadow_compare",
            details={"metric_counts": {"shadow_checks": 1}},
        ),
    )

    summary = assemble_trace(
        record(),
        TraceFacts(debug_payloads={item.span_key: item for item in entries}),
        capability=FULL,
    )["processing_summary"]

    assert summary["duckdb_metrics"] == {
        "query_attempts": 4,
        "queries_completed": 1,
        "queries_failed": 1,
        "queries_denied": 1,
        "queries_cancelled": 1,
        "table_registrations": 1,
        "table_replacements": 1,
    }
    assert summary["cache_metrics"]["shadow_checks"] == 1


def test_invalid_metric_counts_cannot_become_observed_cache_statistics() -> None:
    cached = local_payload(
        details={
            "metric_counts": {
                "cache_reads": "3",
                "storage_hits": True,
                "adopted_snapshots": -1,
            }
        }
    )

    summary = assemble_trace(
        record(), TraceFacts(debug_payloads={cached.span_key: cached}), capability=FULL
    )["processing_summary"]

    assert summary["cache_metrics"]["cache_reads"] is None
    assert summary["cache_metrics"]["storage_hits"] is None
    assert summary["cache_metrics"]["adopted_snapshots"] is None
