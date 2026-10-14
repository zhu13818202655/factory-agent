import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

import factory_agent.application.cache as cache_module
import factory_agent.observability.local_trace as local_trace
from factory_agent.application.cache import AuthAwareCache
from factory_agent.data_api.result_cache import (
    CacheableRequest,
    ResultCacheExecutor,
    ResultCacheMode,
    V2ResultCache,
    footer_reconciles,
)
from factory_agent.domain import EmployeeId, NarrowedFilters, Role, TenantId
from factory_agent.domain.errors import ForbiddenError
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.observability.debug_trace import (
    CaptureScope,
    DebugTraceCapture,
    close_capture_scope,
    configure_debug_trace,
    drain_debug_captures,
    open_capture_scope,
    suspend_debug_capture,
)
from factory_agent.ports.contracts import ResourceFetchResult
from tests.support.payload import as_dict

NOW = datetime(2026, 10, 14, 4, tzinfo=UTC)
TENANT = TenantId("synthetic-cache-tenant")
CST = timezone(timedelta(hours=8))
WINDOW = ("2026-07-01", "2026-07-03")
PARAMS = {"Flag": "0", "Type": "0,1,2"}


@pytest.fixture(autouse=True)
def capture_scope() -> Iterator[None]:
    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(CaptureScope("synthetic-tenant", "synthetic-user", "session", "interaction"))
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


class Store:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.values: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.writes: list[tuple[str, int]] = []
        self.invalidations: list[str] = []
        self.unavailable = unavailable

    async def get(self, key: str) -> bytes | None:
        self.reads.append(key)
        if self.unavailable:
            raise RuntimeError("synthetic-error-credential-canary")
        return self.values.get(key)

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.writes.append((key, ttl_seconds))
        if self.unavailable:
            raise RuntimeError("synthetic-error-credential-canary")
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        self.invalidations.append(prefix)
        if self.unavailable:
            raise RuntimeError("synthetic-error-credential-canary")
        for key in tuple(self.values):
            if key.startswith(prefix):
                del self.values[key]


class Source:
    def __init__(self, rows: tuple[dict[str, Any], ...] = (), *, denied: bool = False) -> None:
        self.rows = rows
        self.calls = 0
        self.denied = denied

    def validate_step(
        self, filters: NarrowedFilters, request: CacheableRequest, active_scope: Any = None
    ) -> None:
        if self.denied:
            raise ForbiddenError("synthetic-denied")

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        self.calls += 1
        return ResourceFetchResult(self.rows, len(self.rows), 1, True)


def make_cache(store: Store | None = None) -> tuple[AuthAwareCache, V2ResultCache, Store]:
    store = store or Store()
    cache = AuthAwareCache(
        store, contract_version="synthetic", metric_version="synthetic", data_version="synthetic"
    )
    return (
        cache,
        V2ResultCache(cache, mode=ResultCacheMode.READWRITE, clock=lambda: NOW, timezone=CST),
        store,
    )


def request(role: Role = Role.OWNER) -> ExecutionRequest:
    return ExecutionRequest(
        operation_id="GongziClQuery",
        time_range=(datetime(2026, 7, 1, tzinfo=CST), datetime(2026, 7, 4, tzinfo=CST)),
        role=role,
    )


def captures_for(
    captures: tuple[DebugTraceCapture, ...], operation: str
) -> list[DebugTraceCapture]:
    return [capture for capture in captures if capture.operation_id == operation]


def details(capture: DebugTraceCapture) -> dict[str, Any]:
    return as_dict(as_dict(capture.payload.output)["details"])


def metadata(capture: DebugTraceCapture) -> dict[str, Any]:
    return as_dict(as_dict(capture.payload.output)["_trace"])


def counters(captures: tuple[DebugTraceCapture, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for capture in captures:
        for name, count in as_dict(details(capture).get("metric_counts", {})).items():
            assert isinstance(count, int)
            result[name] = result.get(name, 0) + count
    return result


@pytest.mark.asyncio
async def test_store_operations_record_actual_ttl_bytes_and_irreversible_key_only() -> None:
    cache, _, store = make_cache()
    params = {"synthetic-sensitive-field": "synthetic-parameter-canary"}
    assert await cache.put(
        "v2_result", TENANT, "synthetic-scope-canary", params, b"value-canary", ttl_seconds=37
    )
    assert (
        await cache.get("v2_result", TENANT, "synthetic-scope-canary", params)
    ).value == b"value-canary"
    assert (await cache.get("v2_result", TENANT, "other", params)).value is None
    await cache.invalidate_key(next(iter(store.values)))
    captures = drain_debug_captures()
    assert counters(captures) == {
        "cache_reads": 2,
        "storage_hits": 1,
        "writes_attempted": 1,
        "writes_succeeded": 1,
    }
    write_input = as_dict(captures_for(captures, "cache.write")[0].payload.input)
    assert write_input["ttl_seconds"] == store.writes[0][1] == 37
    assert write_input["value_bytes"] == len(b"value-canary")
    assert len(write_input["key_digest"]) == 64
    assert details(captures_for(captures, "cache.invalidate")[0])["outcome"] == "invalidated"
    serialized = json.dumps([capture.payload for capture in captures], default=str)
    for value in (
        str(TENANT),
        "synthetic-scope-canary",
        "synthetic-parameter-canary",
        "value-canary",
    ):
        assert value not in serialized


@pytest.mark.asyncio
async def test_store_failure_is_distinct_from_miss_and_keeps_exception_text_private() -> None:
    cache, _, _ = make_cache(Store(unavailable=True))
    assert (await cache.get("v2_result", TENANT, "shared", {})).reason == "store_unavailable"
    assert not await cache.put("v2_result", TENANT, "shared", {}, b"value")
    await cache.invalidate_key("synthetic-key-canary")
    captures = drain_debug_captures()
    assert counters(captures) == {"cache_reads": 1, "writes_attempted": 1, "writes_failed": 1}
    assert all(metadata(capture)["status"] == "error" for capture in captures)
    assert "synthetic-error-credential-canary" not in json.dumps(captures, default=str)
    assert "synthetic-key-canary" not in json.dumps(captures, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("suppressed", [False, True])
async def test_gate_does_not_construct_key_metadata(
    monkeypatch: pytest.MonkeyPatch, suppressed: bool
) -> None:
    cache, _, _ = make_cache()

    def fail_digest(key: str) -> str:
        raise AssertionError("diagnostic payload must not be constructed")

    monkeypatch.setattr(cache_module, "_key_digest", fail_digest)
    if not suppressed:
        configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    with (
        suspend_debug_capture()
        if suppressed
        else local_trace.trace_local_operation("cache", "unused")
    ):
        assert await cache.put("v2_result", TENANT, "shared", {}, b"value")
        assert (await cache.get("v2_result", TENANT, "shared", {})).value == b"value"
    assert drain_debug_captures() == ()


@pytest.mark.asyncio
async def test_capture_failure_does_not_change_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    cache, _, _ = make_cache()

    def fail_capture(**arguments: Any) -> None:
        raise RuntimeError("synthetic-diagnostic-fault")

    monkeypatch.setattr(local_trace, "record_debug_capture", fail_capture)
    assert await cache.put("v2_result", TENANT, "shared", {}, b"value")
    assert (await cache.get("v2_result", TENANT, "shared", {})).value == b"value"
    assert drain_debug_captures() == ()


@pytest.mark.asyncio
async def test_candidates_reject_version_and_keep_probe_order_without_adoption() -> None:
    cache, result_cache, store = make_cache()
    rows = ({"uid": "worker", "inputdate": "2026-07-01", "je": "2"},)
    prefetch = result_cache.prefetch_window("2026-10-14")
    assert await result_cache.write(
        TENANT, "GongziClQuery", rows, None, PARAMS, prefetch, source="precompute"
    )
    key = next(iter(store.values))
    payload = as_dict(json.loads(store.values[key]))
    payload["v"] = -1
    store.values[key] = json.dumps(payload).encode()
    drain_debug_captures()
    assert await result_cache.read(TENANT, "GongziClQuery", PARAMS, WINDOW) is None
    captures = drain_debug_captures()
    candidates = captures_for(captures, "cache.candidate")
    assert [as_dict(candidate.payload.input)["candidate_order"] for candidate in candidates] == [
        1,
        2,
        3,
    ]
    assert metadata(candidates[0])["reason"] == "version_mismatch"
    assert details(candidates[0])["outcome"] == "rejected"
    assert counters(captures) == {"cache_reads": 3, "storage_hits": 1, "candidate_probes": 3}
    assert len(store.reads) == 3
    assert cache.stats.hits["v2_result"] == 1


@pytest.mark.asyncio
async def test_scoped_snapshot_records_actual_projection_and_aggregation_without_raw_values() -> (
    None
):
    _, result_cache, _ = make_cache()
    rows = (
        {
            "uid": "allowed-worker",
            "dept": "private-dept",
            "inputdate": "2026-07-01",
            "jhdh": "allowed-order",
            "je": "987654.32",
            "price": "481.37",
            "uname": "private-name",
        },
        {
            "uid": "allowed-worker",
            "dept": "private-dept",
            "inputdate": "2026-07-02",
            "jhdh": "other-order",
            "je": "1",
        },
        {
            "uid": "allowed-worker",
            "dept": "private-dept",
            "inputdate": "2026-06-30",
            "jhdh": "allowed-order",
            "je": "1",
        },
        {
            "uid": "outside-worker-canary",
            "dept": "outside-dept-canary",
            "inputdate": "2026-07-01",
            "jhdh": "allowed-order",
            "je": "1",
        },
    )
    assert await result_cache.write(
        TENANT,
        "GongziClQuery",
        rows,
        {"je_total": "987657.32"},
        PARAMS,
        result_cache.prefetch_window("2026-10-14"),
        source="precompute",
    )
    drain_debug_captures()
    source = Source()
    executor = ResultCacheExecutor(source, result_cache)
    filters = NarrowedFilters(
        TENANT,
        frozenset({EmployeeId("allowed-worker")}),
        None,
        order_codes=frozenset({"allowed-order"}),
    )
    result = await executor.execute_full_step(
        filters, request(Role.EMPLOYEE), extra_params={**PARAMS, "groupbyFields": ["dept"]}
    )
    assert result.complete and result.total == 1
    assert result.rows[0]["je"] == "987654.32"
    assert source.calls == 0
    captures = drain_debug_captures()
    scope = captures_for(captures, "cache.scope_filter")[0]
    assert as_dict(scope.payload.input)["input_rows"] is None
    assert as_dict(scope.payload.input)["input_rows_state"] == "hidden"
    assert details(scope)["output_rows"] == 3
    assert details(captures_for(captures, "cache.time_filter")[0])["output_rows"] == 2
    assert details(captures_for(captures, "cache.business_filter")[0])["output_rows"] == 1
    aggregate = captures_for(captures, "cache.aggregate")[0]
    assert as_dict(aggregate.payload.input)["input_rows"] == details(aggregate)["output_rows"] == 1
    assert details(aggregate)["groupby_fields"] == ["dept"]
    assert counters(captures)["adopted_snapshots"] == 1
    assert metadata(captures_for(captures, "cache.fetch")[0])["timing_role"] == "parent"
    serialized = json.dumps(captures, default=str)
    for value in (
        "outside-worker-canary",
        "outside-dept-canary",
        "private-name",
        "private-dept",
        "987654.32",
        "481.37",
        "987657.32",
        "allowed-order",
        "other-order",
    ):
        assert value not in serialized


@pytest.mark.asyncio
async def test_cold_result_records_realtime_choice_and_successful_write() -> None:
    _, cache, store = make_cache()
    source = Source(({"uid": "worker", "inputdate": "2026-07-01", "je": "2"},))
    result = await ResultCacheExecutor(source, cache).execute_full_step(
        NarrowedFilters(TENANT, None, None), request(), extra_params=PARAMS
    )
    assert result.complete and result.total == 1 and source.calls == 1
    captures = drain_debug_captures()
    counts = counters(captures)
    assert (
        counts["realtime_fallbacks"]
        == counts["writes_attempted"]
        == counts["writes_succeeded"]
        == 1
    )
    assert counts.get("adopted_snapshots", 0) == 0
    assert len(store.values) == 1
    validation = captures_for(captures, "cache.footer_validate")
    assert all(details(capture)["outcome"] == "not_applicable" for capture in validation)


@pytest.mark.asyncio
async def test_denied_validation_never_reads_writes_or_calls_mes() -> None:
    _, cache, store = make_cache()
    source = Source(denied=True)
    with pytest.raises(ForbiddenError):
        await ResultCacheExecutor(source, cache).execute_full_step(
            NarrowedFilters(TENANT, None, None), request(), extra_params=PARAMS
        )
    assert source.calls == 0
    assert store.reads == store.writes == []
    assert drain_debug_captures() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "footer, reason", [(None, "invalid_time_anchor"), ({"je_total": "9"}, "footer_mismatch")]
)
async def test_write_gates_report_skipped_without_fabricating_store_attempts(
    footer: dict[str, str] | None, reason: str
) -> None:
    _, cache, store = make_cache()
    assert not await cache.write(
        TENANT, "GongziClQuery", ({"je": "1"},), footer, PARAMS, WINDOW, source="query"
    )
    captures = drain_debug_captures()
    publish = captures_for(captures, "cache.publish")[0]
    assert metadata(publish)["reason"] == reason
    assert metadata(publish)["status"] == "skipped"
    assert counters(captures) == {"writes_skipped": 1}
    assert store.writes == []


def test_footer_absent_unavailable_columns_and_success_are_distinct() -> None:
    assert footer_reconciles(({"sl": "3"},), None)
    assert footer_reconciles(({"sl": "3"},), {"je_total": "8"})
    assert footer_reconciles(({"sl": "3"},), {"sl_total": "3"})
    captures = captures_for(drain_debug_captures(), "cache.footer_validate")
    assert [details(capture)["outcome"] for capture in captures] == [
        "not_applicable",
        "not_applicable",
        "accepted",
    ]
    assert details(captures[1])["skipped_columns"] == 1
    assert details(captures[2])["checked_columns"] == 1


@pytest.mark.asyncio
async def test_native_grouped_progress_observes_only_actual_scope_and_business_filters() -> None:
    auth_cache, _, _ = make_cache()
    cache = V2ResultCache(auth_cache, mode=ResultCacheMode.OFF, clock=lambda: NOW, timezone=CST)
    source = Source(
        (
            {"jhdh": "selected-plan", "rq": "2026-07-01", "dept": "D1", "fhsl": "4", "sssl": "2"},
            {"jhdh": "other-plan", "rq": "2026-07-01", "dept": "D1", "fhsl": "8", "sssl": "2"},
        )
    )
    result = await ResultCacheExecutor(source, cache).execute_full_step(
        NarrowedFilters(TENANT, None, None, order_codes=frozenset({"selected-plan"})),
        ExecutionRequest(
            operation_id="ScjdQuery", time_range=request().time_range, role=Role.OWNER
        ),
        extra_params={
            "groupbyFields": ["dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"]
        },
    )
    assert result.complete and result.total == 1 and source.calls == 1
    captures = drain_debug_captures()
    scope = captures_for(captures, "cache.scope_filter")[0]
    business = captures_for(captures, "cache.business_filter")[0]
    assert as_dict(scope.payload.input)["source_form"] == "native_mes_grouped"
    assert details(scope)["output_rows"] == 2
    assert as_dict(business.payload.input)["input_rows"] == 2
    assert details(business)["output_rows"] == 1
    assert not captures_for(captures, "cache.time_filter")
    assert not captures_for(captures, "cache.aggregate")
