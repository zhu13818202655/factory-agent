import json
from collections.abc import Iterator

import pytest

from factory_agent.application.capability_map import RECIPE_BY_FR
from factory_agent.data_api.result_cache import ResultCacheExecutor, ResultCacheMode
from factory_agent.domain import Role
from factory_agent.execution.sandbox_runtime import InteractionSandbox, SandboxTable
from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
)
from tests.unit.data_api.test_result_cache_trace import captures_for, counters, details, metadata
from tests.unit.execution.test_kernel_shared_cache import (
    WAGES,
    LiveExecutor,
    request_for,
    runner,
    seed_snapshots,
)


@pytest.fixture(autouse=True)
def capture_scope() -> Iterator[None]:
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(CaptureScope("synthetic-tenant", "synthetic-user", "session", "interaction"))
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [False, True])
async def test_final_shadow_records_actual_source_and_adopts_only_matching_snapshots(
    mismatch: bool,
) -> None:
    wages = tuple(
        {**row, "inputdate": "2026-07-04"} if mismatch and row["id"] == "R2" else row
        for row in WAGES
    )
    cache, store = await seed_snapshots(ResultCacheMode.SHADOW, wages)
    drain_debug_captures()
    live = LiveExecutor()
    result = await runner(ResultCacheExecutor(live, cache)).run(
        request_for("FR-013", RECIPE_BY_FR["FR-013"], Role.OWNER)
    )
    captures = drain_debug_captures()
    shadow = captures_for(captures, "cache.shadow")[0]
    assert details(shadow)["outcome"] == ("mismatch" if mismatch else "match")
    assert details(shadow)["source"] == ("realtime" if mismatch else "cache")
    assert metadata(shadow)["timing_role"] == "parent"
    assert all(
        details(adoption)["outcome"] == "pending_shadow"
        for adoption in captures_for(captures, "cache.adopt")
    )
    counts = counters(captures)
    assert counts["shadow_checks"] == 1
    assert counts.get("adopted_snapshots", 0) == (0 if mismatch else 2)
    assert len(live.calls) == 2
    if mismatch:
        assert counts["shadow_mismatches"] == 1
        assert result.cache_source is None
        assert store.values == {}
        assert len(captures_for(captures, "cache.invalidate")) == 2
    else:
        assert counts["shadow_matches"] == 1
        assert result.cache_source is not None
        assert len(store.values) == 2


@pytest.mark.asyncio
async def test_unavailable_final_shadow_records_attempt_without_claiming_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, store = await seed_snapshots(ResultCacheMode.SHADOW)
    drain_debug_captures()

    def fail_replace(sandbox: InteractionSandbox, table: SandboxTable) -> None:
        raise RuntimeError("synthetic-shadow-error-canary")

    monkeypatch.setattr(InteractionSandbox, "replace_table", fail_replace)
    with pytest.raises(RuntimeError, match="synthetic-shadow-error-canary"):
        await runner(ResultCacheExecutor(LiveExecutor(), cache)).run(
            request_for("FR-013", RECIPE_BY_FR["FR-013"], Role.OWNER)
        )
    captures = drain_debug_captures()
    shadow = captures_for(captures, "cache.shadow")[0]
    assert details(shadow)["outcome"] == "unavailable"
    assert metadata(shadow)["status"] == "error"
    counts = counters(captures)
    assert counts["shadow_checks"] == counts["shadow_unavailable"] == 1
    assert counts.get("adopted_snapshots", 0) == 0
    assert len(store.values) == 2
    assert "synthetic-shadow-error-canary" not in json.dumps(captures, default=str)
