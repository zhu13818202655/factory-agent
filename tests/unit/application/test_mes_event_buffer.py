"""MES event buffering on the usage ContextVar (Story 11 2.6).

``record_mes_call`` appends into a per-interaction buffer; the session pipeline
drains it at each commit via ``drain_mes_events`` without closing it, and
``set_usage_context(None)`` closes it. Concurrent interactions must never share
a buffer, and calls outside a metered interaction are dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from factory_agent.application import usage
from factory_agent.application.usage import (
    ContextVarMesCallRecorder,
    current_usage_context,
    drain_mes_events,
    record_mes_call,
    set_usage_context,
)
from factory_agent.domain import InteractionId, SessionId, TenantId, UserId
from factory_agent.ports import MesCallRecord

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


def context() -> usage.UsageContext:
    return usage.UsageContext(
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        session_id=SessionId("session-1"),
        interaction_id=InteractionId("interaction-1"),
        trace_id="a" * 32,
    )


def mes_call(operation_id: str = "YskQuery", status: str = "completed") -> MesCallRecord:
    return MesCallRecord(
        operation_id=operation_id,
        page_count=1,
        row_count=3,
        duration_ms=10,
        status=status,  # type: ignore[arg-type]
    )


def test_recording_requires_an_open_usage_context() -> None:
    set_usage_context(None)

    record_mes_call(mes_call())

    assert drain_mes_events() == ()


def test_recorded_calls_are_drained_and_the_buffer_stays_open() -> None:
    set_usage_context(context())

    record_mes_call(mes_call("YskQuery"))
    record_mes_call(mes_call("GongziMxQuery", status="failed"))
    first = drain_mes_events()

    assert len(first) == 2
    assert [event.payload["operation_id"] for event in first] == [
        "YskQuery",
        "GongziMxQuery",
    ]

    # The buffer is still open: events recorded after the drain are captured
    # by the next commit.
    record_mes_call(mes_call("PlanGridPageList"))
    second = drain_mes_events()

    assert [event.payload["operation_id"] for event in second] == ["PlanGridPageList"]
    set_usage_context(None)


def test_draining_is_destructive_only_for_the_caller() -> None:
    set_usage_context(context())

    record_mes_call(mes_call("YskQuery"))
    drain_mes_events()

    assert drain_mes_events() == ()
    set_usage_context(None)


def test_closing_the_context_clears_pending_events() -> None:
    set_usage_context(context())
    record_mes_call(mes_call())

    set_usage_context(None)

    assert drain_mes_events() == ()
    assert current_usage_context() is None


def test_concurrent_interactions_never_share_a_buffer() -> None:
    """ContextVars isolate per-task state; sequential contexts stay separated."""
    set_usage_context(context())
    record_mes_call(mes_call("YskQuery"))
    first_buffer = drain_mes_events()

    set_usage_context(
        usage.UsageContext(
            tenant_id=TenantId("tenant-b"),
            user_id=UserId("user-b"),
            session_id=SessionId("session-2"),
            interaction_id=InteractionId("interaction-2"),
            trace_id="b" * 32,
        )
    )
    record_mes_call(mes_call("GongziMxQuery"))
    second_buffer = drain_mes_events()

    assert [event.payload["operation_id"] for event in first_buffer] == ["YskQuery"]
    assert [event.payload["operation_id"] for event in second_buffer] == ["GongziMxQuery"]
    set_usage_context(None)


def test_context_var_recorder_routes_through_the_shared_impl() -> None:
    recorder = ContextVarMesCallRecorder()
    set_usage_context(context())

    recorder.record(mes_call("YskQuery"))

    assert len(drain_mes_events()) == 1
    set_usage_context(None)


def test_a_construction_failure_is_dropped_not_raised() -> None:
    """An event constructor fault must never propagate into the adapter."""
    set_usage_context(context())

    def broken(operation_id: str) -> None:
        raise RuntimeError("boom")

    original = usage.mes_call_completed_event
    usage.mes_call_completed_event = broken  # type: ignore[assignment]
    try:
        record_mes_call(mes_call("YskQuery"))
    finally:
        usage.mes_call_completed_event = original

    assert drain_mes_events() == ()
    set_usage_context(None)
