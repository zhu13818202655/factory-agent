from datetime import datetime, timezone
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from factory_agent.persistence.debug_trace_store import SqlDebugTraceStore

NOW = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)


def _engine_with_rows(
    rows: list[dict[str, object]],
) -> tuple[AsyncEngine, AsyncMock]:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    connection = AsyncMock(spec=AsyncConnection)
    connection.execute.return_value = result
    engine = MagicMock(spec=AsyncEngine)
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
    return cast(AsyncEngine, engine), connection


@pytest.mark.asyncio
async def test_debug_payload_read_carries_local_kind_timing_anchor_and_ownership() -> None:
    timing = {"_trace": {"started_at": NOW.isoformat(), "duration_ms": 10}, "details": {}}
    engine, connection = _engine_with_rows(
        [
            {
                "span_key": "duckdb:q1",
                "input_payload": {"sql": "SELECT 1"},
                "output_payload": timing,
                "truncated": False,
                "original_rows": None,
                "original_bytes": 90,
                "kind": "duckdb",
                "operation_id": "query",
                "stage": "query",
                "created_at": NOW,
            }
        ]
    )

    payloads = await SqlDebugTraceStore(engine).load_payloads(
        tenant_id="tenant-a", user_id="user-a", interaction_id="int-a", now=NOW
    )

    found = payloads["duckdb:q1"]
    assert found.kind == "duckdb"
    assert found.operation_id == "query"
    assert found.stage == "query"
    assert found.occurred_at == NOW
    assert found.output == timing
    assert found.original_bytes == 90
    statement = cast(sa.sql.ClauseElement, connection.execute.call_args.args[0])
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "debug_trace.tenant_id = 'tenant-a'" in sql
    assert "debug_trace.user_id = 'user-a'" in sql
    assert "debug_trace.interaction_id = 'int-a'" in sql
    assert "debug_trace.expires_at >=" in sql
    assert "debug_trace.kind" in sql
    assert "debug_trace.operation_id" in sql
    assert "debug_trace.stage" in sql
    assert "ORDER BY debug_trace.created_at, debug_trace.trace_id" in sql


@pytest.mark.asyncio
async def test_legacy_debug_payload_values_have_compatible_metadata_defaults() -> None:
    engine, _ = _engine_with_rows(
        [
            {
                "span_key": "mes:x",
                "input_payload": {"a": 1},
                "output_payload": {"b": 2},
                "truncated": True,
                "original_rows": 20,
                "original_bytes": 200,
            }
        ]
    )

    payloads = await SqlDebugTraceStore(engine).load_payloads(
        tenant_id="tenant-a", user_id="user-a", interaction_id="int-a", now=NOW
    )

    found = payloads["mes:x"]
    assert found.kind is None
    assert found.operation_id is None
    assert found.stage is None
    assert found.occurred_at is None
    assert found.input == {"a": 1}
    assert found.truncated is True
