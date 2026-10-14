import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
)
from factory_agent.observability.local_trace import trace_local_operation
from factory_agent.persistence.debug_trace_store import SqlDebugTraceStore
from factory_agent.persistence.engine import normalize_dsn
from factory_agent.persistence.tables import debug_trace_table
from tests.support.payload import as_dict

DATABASE_URL = os.environ.get("FACTORY_AGENT_TEST_POSTGRES_URL")
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
pytestmark = [
    pytest.mark.skipif(not DATABASE_URL, reason="requires explicit disposable test PostgreSQL"),
    pytest.mark.asyncio,
]


@pytest.fixture(autouse=True)
def gate() -> Iterator[None]:
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert DATABASE_URL is not None
    schema = "test_local_trace_" + uuid4().hex
    created = create_async_engine(
        normalize_dsn(DATABASE_URL),
        poolclass=sa.pool.NullPool,
        execution_options={"schema_translate_map": {None: schema}},
    )
    try:
        async with created.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(schema))
            await connection.run_sync(debug_trace_table.create)
        yield created
    finally:
        async with created.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema, cascade=True))
        await created.dispose()


async def test_local_span_payload_round_trip_is_owned_bounded_and_expires(
    engine: AsyncEngine,
) -> None:
    store = SqlDebugTraceStore(engine, retention_hours=24)
    configure_debug_trace(enabled=True, max_payload_bytes=1024, max_rows=5)
    open_capture_scope(CaptureScope("tenant-a", "user-a", "session-a", "interaction-a"))
    with trace_local_operation(
        "duckdb",
        "duckdb.query",
        now=lambda: NOW,
        input_payload=lambda: {
            "sql": "SELECT " + "quantity + " * 1000 + "0 FROM detail",
            "authorization": "Bearer TOKEN-CANARY-postgres",
        },
    ) as trace:
        trace.set_output(output_rows=1)
    await store.write(drain_debug_captures())
    close_capture_scope()
    rows = await store.load_payloads(
        tenant_id="tenant-a",
        user_id="user-a",
        interaction_id="interaction-a",
        now=NOW,
    )
    assert len(rows) == 1
    (payload,) = rows.values()
    assert payload.kind == "duckdb"
    assert payload.operation_id == "duckdb.query"
    assert payload.truncated
    assert as_dict(as_dict(payload.output)["details"])["output_rows"] == 1
    assert as_dict(as_dict(payload.output)["_trace"])["status"] == "ok"
    assert "TOKEN-CANARY" not in str(payload.input)
    for tenant, user, moment in (
        ("tenant-b", "user-a", NOW),
        ("tenant-a", "user-b", NOW),
        ("tenant-a", "user-a", NOW + timedelta(hours=25)),
    ):
        assert (
            await store.load_payloads(
                tenant_id=tenant,
                user_id=user,
                interaction_id="interaction-a",
                now=moment,
            )
            == {}
        )
