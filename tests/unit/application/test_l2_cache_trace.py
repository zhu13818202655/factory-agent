import json
from collections.abc import Iterator
from datetime import datetime

import pytest
from sageql_core import ActionRejected, Filter, Query

from factory_agent.domain import Role
from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
)
from tests.unit.data_api.test_result_cache_trace import captures_for, counters, details
from tests.unit.execution.test_analysis_host import Facts, control_for, ensure, host_for


@pytest.fixture(autouse=True)
def capture_scope() -> Iterator[None]:
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(CaptureScope("synthetic-tenant", "synthetic-user", "session", "interaction"))
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


@pytest.mark.asyncio
async def test_complete_empty_exact_reuse_records_zero_rows_without_second_fetch() -> None:
    facts = Facts()
    host = host_for(facts)
    control = control_for(host)
    query = Query(dataset_id="reported_production", columns=("quantity",))
    try:
        original = await ensure(host, query, control)
        drain_debug_captures()
        reused = await ensure(host, query, control)
        assert original.complete and reused.complete
        assert reused.dataset_refs == original.dataset_refs
        assert reused.row_count == 0
        assert len(facts.calls) == 1
        captures = drain_debug_captures()
        assert counters(captures) == {"interaction_reuses": 1, "exact_reuses": 1}
        span = captures_for(captures, "cache.interaction_exact")[0]
        assert details(span)["output_rows"] == 0
        assert details(span)["outcome"] == "reuse"
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_covering_reuse_records_actual_filter_counts_without_condition_values() -> None:
    facts = Facts(
        (
            {
                "plan_code": "synthetic-selected-plan",
                "bed_code": "1555",
                "business_date": "2026-09-02",
                "quantity": "71",
            },
            {
                "plan_code": "synthetic-other-plan-canary",
                "bed_code": "1555",
                "business_date": "2026-09-02",
                "quantity": "81",
            },
            {
                "plan_code": "synthetic-selected-plan",
                "bed_code": "1555",
                "business_date": "2026-09-20",
                "quantity": "91",
            },
        )
    )
    host = host_for(facts)
    control = control_for(host)
    try:
        initial = await ensure(
            host,
            Query(
                dataset_id="reported_production", columns=("quantity", "plan_code", "business_date")
            ),
            control,
        )
        drain_debug_captures()
        reused = await ensure(
            host,
            Query(
                dataset_id="reported_production",
                columns=("quantity", "plan_code", "business_date"),
                filters=(Filter(field="plan_code", values=("synthetic-selected-plan",)),),
                window_start=datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
                window_end=datetime.fromisoformat("2026-09-10T00:00:00+08:00"),
            ),
            control,
        )
        assert initial.row_count == 3 and reused.row_count == 1
        assert reused.complete and len(facts.calls) == 1
        assert reused.dataset_refs != initial.dataset_refs
        captures = drain_debug_captures()
        counts = counters(captures)
        assert counts["interaction_reuses"] == counts["covering_reuses"] == 1
        assert counts.get("exact_reuses", 0) == counts.get("cache_reads", 0) == 0
        projection = captures_for(captures, "cache.interaction_filter")[0]
        assert details(projection)["output_rows"] == 1
        serialized = json.dumps(captures, default=str)
        for value in ("synthetic-selected-plan", "synthetic-other-plan-canary"):
            assert value not in serialized
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_unauthorized_dataset_records_no_reuse_and_performs_zero_fetches() -> None:
    facts = Facts()
    host = host_for(facts, role=Role.EMPLOYEE)
    control = control_for(host)
    try:
        with pytest.raises(ActionRejected):
            await ensure(host, Query(dataset_id="production_progress"), control)
        assert facts.calls == []
        assert drain_debug_captures() == ()
    finally:
        host.close()
        host.sandbox.close()
