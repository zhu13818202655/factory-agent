import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
    suspend_debug_capture,
)
from factory_agent.observability.local_trace import safe_sql_payload, trace_local_operation
from tests.support.payload import as_dict

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def capture_gate() -> Iterator[None]:
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


def enable() -> None:
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(CaptureScope("tenant-a", "user-a", "session-a", "interaction-a"))


def test_disabled_or_unowned_capture_does_not_build_payload() -> None:
    def forbidden() -> object:
        raise AssertionError("payload must not be built")

    with trace_local_operation("cache", "cache.lookup", input_payload=forbidden) as trace:
        assert not trace.active
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    with trace_local_operation("duckdb", "duckdb.query", input_payload=forbidden) as trace:
        assert not trace.active
    assert drain_debug_captures() == ()


def test_local_span_has_measured_duration_ownership_and_safe_content() -> None:
    enable()
    moments = iter((2.0, 2.125))
    with trace_local_operation(
        "cache",
        "cache.lookup",
        input_payload=lambda: {"domain": "output"},
        now=lambda: NOW,
        monotonic=lambda: next(moments),
        new_id=lambda: "lookup-1",
    ) as trace:
        trace.set_output(outcome="hit", metric_counts={"cache_reads": 1})
    (captured,) = drain_debug_captures()
    metadata = as_dict(as_dict(captured.payload.output)["_trace"])
    assert captured.span_key == "cache:lookup-1"
    assert captured.tenant_id == "tenant-a"
    assert captured.user_id == "user-a"
    assert captured.kind == "cache"
    assert metadata["started_at"] == NOW.isoformat()
    assert metadata["duration_ms"] == 125
    assert metadata["timing_role"] == "leaf"
    assert as_dict(captured.payload.input)["domain"] == "output"


def test_parent_relationship_does_not_require_shared_global_state() -> None:
    enable()
    with trace_local_operation("cache", "cache.candidate", timing_role="parent"):
        with trace_local_operation("cache", "cache.lookup"):
            pass
    child, parent = drain_debug_captures()
    assert as_dict(as_dict(child.payload.output)["_trace"])["parent_span_id"] == parent.span_key
    assert as_dict(as_dict(parent.payload.output)["_trace"])["timing_role"] == "parent"
    with trace_local_operation("duckdb", "duckdb.query"):
        pass
    (independent,) = drain_debug_captures()
    assert as_dict(as_dict(independent.payload.output)["_trace"])["parent_span_id"] is None


@pytest.mark.asyncio
async def test_worker_captures_reach_only_the_current_interaction_buffer() -> None:
    enable()

    def worker() -> None:
        with trace_local_operation("duckdb", "duckdb.query") as trace:
            trace.set_output(output_rows=2)

    await asyncio.to_thread(worker)
    (capture,) = drain_debug_captures()
    assert capture.interaction_id == "interaction-a"
    assert as_dict(as_dict(capture.payload.output)["details"])["output_rows"] == 2


def test_suppressed_capture_never_builds_payload_and_drops_inflight_capture() -> None:
    enable()

    def forbidden() -> object:
        raise AssertionError("suppressed input factory ran")

    with suspend_debug_capture():
        with trace_local_operation("cache", "cache.lookup", input_payload=forbidden):
            pass
    assert drain_debug_captures() == ()
    with suspend_debug_capture():
        with trace_local_operation("duckdb", "duckdb.query"):
            pass
    assert drain_debug_captures() == ()


def test_failure_preserves_original_exception_without_its_message_in_trace() -> None:
    enable()
    exception = ValueError("SECRET-CANARY-local-exception")
    with pytest.raises(ValueError) as failure:
        with trace_local_operation("duckdb", "duckdb.query"):
            raise exception
    assert failure.value is exception
    (capture,) = drain_debug_captures()
    metadata = as_dict(as_dict(capture.payload.output)["_trace"])
    assert metadata["status"] == "error"
    assert metadata["error_category"] == "operation_failed"
    assert "SECRET-CANARY" not in json.dumps(capture.payload.output)


def test_capture_setup_failure_does_not_prevent_business_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    enable()

    def broken() -> object:
        raise ValueError("SECRET-CANARY-factory")

    worked = False
    with trace_local_operation("cache", "cache.lookup", input_payload=broken) as trace:
        worked = True
        assert not trace.active
    assert worked
    assert drain_debug_captures() == ()
    assert "SECRET-CANARY" not in caplog.text


def test_sql_literal_comment_and_scope_bindings_are_not_retained() -> None:
    sql = (
        "SELECT 'APPKEY-CANARY' AS label, $$TOKEN-CANARY$$, $tag$SIGN-CANARY$tag$ "
        "FROM detail WHERE uid IN (SELECT unnest($employee_ids)) "
        "/* outer APPKEY-CANARY /* inner TOKEN-CANARY */ SIGN-CANARY */ "
        "-- Bearer SECRET-CANARY\n"
    )
    payload = safe_sql_payload(sql, {"employee_ids": ["employee-canary"], "salary": 8213.44})
    serialized = json.dumps(payload)
    for canary in (
        "APPKEY-CANARY",
        "TOKEN-CANARY",
        "SIGN-CANARY",
        "SECRET-CANARY",
        "employee-canary",
        "8213.44",
    ):
        assert canary not in serialized
    assert "unnest($employee_ids)" in str(payload["sql"])
    assert payload["sql_redacted"] is True
    assert as_dict(as_dict(payload["parameters"])["employee_ids"])["count"] == 1


def test_sql_without_literals_remains_exact_and_positional_values_stay_hidden() -> None:
    sql = 'SELECT SUM(quantity) AS total FROM "detail" WHERE business_date >= ?'
    payload = safe_sql_payload(sql, ["2026-10-01"])
    assert payload["sql"] == sql
    assert payload["sql_redacted"] is False
    assert "2026-10-01" not in json.dumps(payload)


def test_numeric_literals_are_hidden_without_rewriting_identifier_or_parameter_digits() -> None:
    sql = "SELECT 8213.44 AS gross_amount, 123456 AS employee_id FROM detail_v2 WHERE uid = $id_1"
    payload = safe_sql_payload(sql)
    assert "8213.44" not in str(payload["sql"])
    assert "123456" not in str(payload["sql"])
    assert "detail_v2" in str(payload["sql"])
    assert "$id_1" in str(payload["sql"])
    assert payload["sql_redacted"] is True


def test_truncated_sql_retains_timing_metadata_and_declares_truncation() -> None:
    enable()
    configure_debug_trace(enabled=True, max_payload_bytes=1024, max_rows=5)
    with trace_local_operation(
        "duckdb",
        "duckdb.query",
        input_payload=lambda: {
            "sql": "SELECT " + "quantity + " * 1500 + "0 FROM detail",
        },
    ):
        pass
    (capture,) = drain_debug_captures()
    assert capture.payload.truncated
    assert capture.payload.original_bytes is not None
    assert as_dict(as_dict(capture.payload.output)["_trace"])["status"] == "ok"
