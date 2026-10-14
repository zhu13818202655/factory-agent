import asyncio
import json
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

import duckdb
import pytest

import factory_agent.execution.sandbox_runtime as sandbox_module
from factory_agent.domain.errors import ForbiddenError, InvalidRequestError
from factory_agent.execution.sandbox_runtime import InteractionSandbox
from factory_agent.observability.debug_trace import (
    CaptureScope,
    DebugTraceCapture,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
    suspend_debug_capture,
)
from factory_agent.ports.sandbox import SandboxTable
from tests.support.payload import as_dict

SCOPE = CaptureScope("tenant-synthetic", "user-synthetic", "session-1", "interaction-1")
PRIVATE_ROW = "PRIVATE-ROW-CANARY-7a6f"
SQL_CANARY = "SQL-CREDENTIAL-CANARY-8e4a"


@pytest.fixture(autouse=True)
def _capture_scope() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(SCOPE)
    yield
    close_capture_scope()
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)


def _details(capture: DebugTraceCapture) -> dict[str, object]:
    return as_dict(as_dict(capture.payload.output)["details"])


def _metadata(capture: DebugTraceCapture) -> dict[str, object]:
    return as_dict(as_dict(capture.payload.output)["_trace"])


def _select(captures: tuple[DebugTraceCapture, ...], operation: str) -> DebugTraceCapture:
    matches = [capture for capture in captures if capture.operation_id == operation]
    assert len(matches) == 1
    return matches[0]


def _table(name: str = "records") -> SandboxTable:
    return SandboxTable(
        name,
        ({"employee_id": PRIVATE_ROW, "quantity": 3},),
        (("employee_id", "VARCHAR"), ("quantity", "INTEGER")),
    )


def test_lazy_connection_registration_queries_and_close_have_actual_evidence() -> None:
    sandbox = InteractionSandbox(allowed_tables=("records",))
    assert drain_debug_captures() == ()

    sandbox.register_table(_table())
    sql = "SELECT SUM(quantity) AS quantity FROM records WHERE employee_id = $employee_id"
    assert sandbox.execute(sql, {"employee_id": PRIVATE_ROW}) == [(3,)]
    assert sandbox.execute(sql, {"employee_id": PRIVATE_ROW}) == [(3,)]
    sandbox.close()
    sandbox.close()

    captures = drain_debug_captures()
    assert [capture.operation_id for capture in captures] == [
        "duckdb.connect",
        "duckdb.register",
        "duckdb.query",
        "duckdb.query",
        "duckdb.close",
    ]
    assert len({capture.span_key for capture in captures}) == len(captures)
    assert all(capture.interaction_id == SCOPE.interaction_id for capture in captures)
    assert all(capture.kind == "duckdb" for capture in captures)
    registration = _select(captures, "duckdb.register")
    registration_input = as_dict(registration.payload.input)
    assert registration_input["input_rows"] == 1
    assert registration_input["schema"] == [
        {"column": "employee_id", "type": "VARCHAR"},
        {"column": "quantity", "type": "INTEGER"},
    ]
    assert registration_input["create_sql"] == (
        'CREATE TABLE "records" ("employee_id" VARCHAR, "quantity" INTEGER)'
    )
    assert "CAST(? AS STRUCT(" in registration_input["bulk_insert_sql"]
    assert _details(registration)["load_method"] == "bulk_json"
    assert _details(registration)["output_rows"] == 1
    for query in (capture for capture in captures if capture.operation_id == "duckdb.query"):
        query_input = as_dict(query.payload.input)
        assert query_input["sql"] == sql
        assert query_input["registered_tables"] == {"records": 1}
        assert query_input["duration_basis"] == "execute_and_fetch"
        assert _details(query)["result_columns"] == ["quantity"]
        assert _details(query)["output_rows"] == 1
        assert _metadata(query)["timing_role"] == "leaf"
        assert _metadata(query)["parent_span_id"] is None
    assert _details(_select(captures, "duckdb.close"))["remaining_tables"] == 0
    serialized = json.dumps(
        [(capture.payload.input, capture.payload.output) for capture in captures], default=str
    )
    assert PRIVATE_ROW not in serialized


@pytest.mark.parametrize(
    ("column_type", "value", "reason"),
    [
        ("TINYINT", 3, "unsupported_column_type"),
        ("DECIMAL(18,4)", Decimal("3.25"), "json_serialization_failed"),
    ],
)
def test_per_row_fallback_records_whole_batch_and_finite_reason(
    column_type: str, value: int | Decimal, reason: str
) -> None:
    with InteractionSandbox(allowed_tables=("records",)) as sandbox:
        sandbox.register_table(
            SandboxTable("records", ({"quantity": value},), (("quantity", column_type),))
        )
        assert sandbox.execute("SELECT quantity FROM records") == [(value,)]
    registration = _select(drain_debug_captures(), "duckdb.register")
    assert _details(registration)["fallback_reason"] == reason
    assert _details(registration)["load_method"] == "per_row"
    assert _details(registration)["output_rows"] == 1
    assert _details(registration)["bulk_attempted"] is False
    assert as_dict(registration.payload.input)["insert_sql"] == (
        'INSERT INTO "records" ("quantity") VALUES (?)'
    )


class _FailingConnection:
    def __init__(self, connection: duckdb.DuckDBPyConnection, *, interrupt: bool = False) -> None:
        self.connection = connection
        self.interrupt = interrupt

    def execute(self, sql: str, params: object = None) -> duckdb.DuckDBPyConnection:
        if sql.startswith("INSERT") and "unnest(CAST(" in sql:
            raise duckdb.ConversionException(SQL_CANARY)
        if self.interrupt and sql.startswith("SELECT"):
            raise duckdb.InterruptException(SQL_CANARY)
        return self.connection.execute(sql, params)

    def close(self) -> None:
        self.connection.close()


def test_bulk_execute_failure_falls_back_and_never_captures_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = InteractionSandbox(allowed_tables=("records",))
    connection = _FailingConnection(duckdb.connect())
    monkeypatch.setattr(sandbox, "_connection", cast(duckdb.DuckDBPyConnection, connection))
    with sandbox:
        sandbox.register_table(_table())
        assert sandbox.execute("SELECT quantity FROM records") == [(3,)]
    captures = drain_debug_captures()
    registration = _select(captures, "duckdb.register")
    assert _details(registration)["bulk_attempted"] is True
    assert _details(registration)["fallback_reason"] == "bulk_insert_failed"
    assert _details(registration)["load_method"] == "per_row"
    assert _details(registration)["output_rows"] == 1
    assert SQL_CANARY not in json.dumps(registration.payload.output)


def test_empty_tables_empty_results_and_replacement_parent_are_explicit() -> None:
    with InteractionSandbox(allowed_tables=("records",)) as sandbox:
        sandbox.register_table(SandboxTable("records", (), (("quantity", "INTEGER"),)))
        assert sandbox.execute("SELECT quantity FROM records") == []
        sandbox.replace_table(
            SandboxTable("records", ({"quantity": 4},), (("quantity", "INTEGER"),))
        )
        assert sandbox.execute("SELECT quantity FROM records") == [(4,)]
    captures = drain_debug_captures()
    registrations = [capture for capture in captures if capture.operation_id == "duckdb.register"]
    assert _details(registrations[0])["load_method"] == "empty"
    assert _details(registrations[0])["output_rows"] == 0
    queries = [capture for capture in captures if capture.operation_id == "duckdb.query"]
    assert _details(queries[0])["output_rows"] == 0
    replacement = _select(captures, "duckdb.replace")
    assert _metadata(replacement)["timing_role"] == "parent"
    assert _metadata(registrations[1])["parent_span_id"] == replacement.span_key
    assert as_dict(replacement.payload.input)["previous_rows"] == 0
    assert _details(replacement)["output_rows"] == 1


def test_denied_sql_never_constructs_sql_or_parameter_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_payload(*_: object) -> dict[str, object]:
        pytest.fail("rejected SQL reached the diagnostic payload builder")

    monkeypatch.setattr(sandbox_module, "safe_sql_payload", forbidden_payload)
    sandbox = InteractionSandbox()
    with pytest.raises(ForbiddenError):
        sandbox.execute(f"DELETE FROM records WHERE employee_id = '{SQL_CANARY}'", [SQL_CANARY])
    captures = drain_debug_captures()
    rejection = _select(captures, "duckdb.query")
    assert len(captures) == 1
    assert _metadata(rejection)["status"] == "denied"
    assert _metadata(rejection)["reason"] == "read_only_policy_rejected"
    assert _details(rejection)["execution_attempted"] is False
    assert "sql" not in as_dict(rejection.payload.input)
    assert SQL_CANARY not in json.dumps(rejection.payload.input)


def test_query_masks_literals_comments_bindings_and_sensitive_result_aliases() -> None:
    with InteractionSandbox() as sandbox:
        sql = f"SELECT '{SQL_CANARY}' AS \"{PRIVATE_ROW}\", $employee_ids AS ids -- {SQL_CANARY}"
        assert sandbox.execute(sql, {"employee_ids": [PRIVATE_ROW]}) == [
            (SQL_CANARY, [PRIVATE_ROW])
        ]
    query = _select(drain_debug_captures(), "duckdb.query")
    assert _details(query)["result_columns"] == ["[hidden]", "ids"]
    serialized = json.dumps([query.payload.input, query.payload.output])
    assert SQL_CANARY not in serialized
    assert PRIVATE_ROW not in serialized
    assert as_dict(query.payload.input)["sql_redacted"] is True


def test_execute_df_returns_unconsumed_cursor_with_unavailable_row_count() -> None:
    with InteractionSandbox() as sandbox:
        cursor = sandbox.execute_df("SELECT 1 AS quantity UNION ALL SELECT 2 AS quantity")
        query = _select(drain_debug_captures(), "duckdb.query")
        assert _details(query)["output_rows"] is None
        assert _details(query)["row_count_state"] == "unavailable_cursor_not_consumed"
        assert cursor.fetchall() == [(1,), (2,)]


def test_execution_failure_records_only_safe_category() -> None:
    with InteractionSandbox() as sandbox:
        with pytest.raises(InvalidRequestError):
            sandbox.execute(f'SELECT 1 AS quantity FROM "{SQL_CANARY}"')
    query = _select(drain_debug_captures(), "duckdb.query")
    assert _metadata(query)["status"] == "error"
    assert _metadata(query)["reason"] == "query_execution_failed"
    assert SQL_CANARY not in json.dumps([query.payload.input, query.payload.output])


def test_interrupted_query_preserves_cancelled_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = InteractionSandbox()
    connection = _FailingConnection(duckdb.connect(), interrupt=True)
    monkeypatch.setattr(sandbox, "_connection", cast(duckdb.DuckDBPyConnection, connection))
    with sandbox, pytest.raises(InvalidRequestError):
        sandbox.execute("SELECT 1 AS quantity")
    query = _select(drain_debug_captures(), "duckdb.query")
    assert _metadata(query)["status"] == "cancelled"
    assert _metadata(query)["reason"] == "query_interrupted"
    assert SQL_CANARY not in json.dumps(query.payload.output)


@pytest.mark.parametrize("mode", ["off", "suppressed", "no_scope"])
def test_disabled_or_suppressed_capture_never_builds_query_payload(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def detect_payload(*_: object) -> dict[str, object]:
        calls.append("unexpected")
        return {}

    monkeypatch.setattr(sandbox_module, "safe_sql_payload", detect_payload)
    if mode == "off":
        configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    elif mode == "no_scope":
        close_capture_scope()
    with suspend_debug_capture() if mode == "suppressed" else nullcontext():
        with InteractionSandbox() as sandbox:
            assert sandbox.execute("SELECT 1 AS quantity") == [(1,)]
    assert calls == []
    assert drain_debug_captures() == ()


@pytest.mark.asyncio
async def test_worker_thread_captures_keep_interaction_owner() -> None:
    with InteractionSandbox() as sandbox:
        assert await asyncio.to_thread(sandbox.execute, "SELECT 1 AS quantity") == [(1,)]
    captures = drain_debug_captures()
    assert {capture.interaction_id for capture in captures} == {SCOPE.interaction_id}
    assert {capture.user_id for capture in captures} == {SCOPE.user_id}
    assert _details(_select(captures, "duckdb.query"))["output_rows"] == 1


def test_payload_construction_failure_keeps_query_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_payload(*_: object) -> dict[str, object]:
        raise ValueError(SQL_CANARY)

    monkeypatch.setattr(sandbox_module, "safe_sql_payload", failed_payload)
    with InteractionSandbox() as sandbox:
        assert sandbox.execute("SELECT 1 AS quantity") == [(1,)]
    assert not any(capture.operation_id == "duckdb.query" for capture in drain_debug_captures())


def test_timing_uses_utc_started_instants() -> None:
    with InteractionSandbox() as sandbox:
        assert sandbox.execute("SELECT 1 AS quantity") == [(1,)]
    query = _select(drain_debug_captures(), "duckdb.query")
    started = datetime.fromisoformat(str(_metadata(query)["started_at"]))
    assert started.tzinfo == timezone.utc
    assert isinstance(_metadata(query)["duration_ms"], int)
    assert cast(int, _metadata(query)["duration_ms"]) >= 0
