"""V2 结果集缓存（老板缓存）单元测试：纯函数 + 缓存读写 + 取数层装饰器。

对应 ``src/factory_agent/data_api/result_cache.py`` 与方案
``docs/design/V2接口取数重构与缓存加速方案.md`` §4 / 附录 F。
"""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

import factory_agent.data_api.result_cache as result_cache_module
from factory_agent.application.cache import AuthAwareCache
from factory_agent.application.prefetch import V2PrefetchWorker
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.progress_detail import ProgressDerivationError, normalize_progress_date
from factory_agent.data_api.result_cache import (
    PROGRESS_DATE_COLUMN,
    RESULT_SET_VERSION,
    ResultCacheExecutor,
    ResultCacheMode,
    ResultCachePolicy,
    V2ResultCache,
    cacheable_grain,
    decode_result_set,
    encode_result_set,
    footer_reconciles,
    local_view_rows,
    normalize_params,
    recomputed_footer,
    scope_filtered_rows,
    shadow_compare,
    time_anchor_column,
    time_sliced_rows,
    window_category,
)
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    NarrowedFilters,
    Role,
    ScopeVersion,
    TenantId,
)
from factory_agent.domain.errors import (
    ForbiddenError,
    InvalidRequestError,
    UnsupportedOperationError,
)
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor
from factory_agent.ports.contracts import ResourceFetchResult
from tests.support.execution_kernel import RecordingAdapter

TODAY = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
BACKTRACK_START = "2025-10-15"
YESTERDAY = "2026-10-14"
PREFETCH_WINDOW = (BACKTRACK_START, YESTERDAY)
M7_GROUPBY = ["dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"]

#: 部署时区（与 ``prefetch.run_once`` 的 ``timezone_name`` 同源）。
CST = ZoneInfo("Asia/Shanghai")
#: 2026-10-15 22:00 UTC = 2026-10-16 06:00 CST：UTC 与部署时区的「今天」不同。
LATE_UTC = datetime(2026, 10, 15, 22, 0, 0, tzinfo=timezone.utc)

#: 行集统一类型：生产路径传入 ``dict[str, object]``（值含 str/Decimal/None）。
Rows = tuple[dict[str, object], ...]


class MemoryStore:
    """CacheStore 的内存替身：记录 TTL 供分档断言。"""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.lookups: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.lookups.append(key)
        return self.data.get(key)

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.data[key] = value
        self.ttls[key] = ttl_seconds

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self.data if k.startswith(prefix)]:
            self.data.pop(key, None)
            self.ttls.pop(key, None)


def make_cache(store: MemoryStore | None = None) -> AuthAwareCache:
    return AuthAwareCache(
        store or MemoryStore(),
        contract_version="test-contract",
        metric_version="test-metric",
        data_version="test-data",
    )


def make_result_cache(
    mode: ResultCacheMode = ResultCacheMode.READWRITE,
    policy: ResultCachePolicy | None = None,
    store: MemoryStore | None = None,
    clock: Any = None,
    timezone: Any = None,
) -> V2ResultCache:
    return V2ResultCache(
        make_cache(store),
        mode=mode,
        policy=policy or ResultCachePolicy(backtrack_days=365),
        clock=clock or (lambda: TODAY),
        timezone=timezone,
    )


def owner_filters() -> NarrowedFilters:
    return NarrowedFilters(tenant_id=TenantId("t"), employee_ids=None, dept_ids=None)


# ---------------------------------------------------------------------------
# 纯函数


def test_progress_date_normalizes_explicit_rq_calendar_day():
    assert normalize_progress_date("2026-07-01") == "2026-07-01"
    assert normalize_progress_date("2026-12-31T23:00:00+00:00") == "2026-12-31"


@pytest.mark.parametrize("rq", ["2026-02-30", "20260701001", "2026", "", None, "abcdefgh1"])
def test_progress_date_rejects_invalid_rq(rq: Any):
    with pytest.raises(ProgressDerivationError, match="invalid_progress_date"):
        normalize_progress_date(rq)


def test_window_category_three_paths():
    assert window_category("2026-07-01", "2026-07-02", "2026-10-15") == "settled"
    assert window_category("2026-10-15", "2026-10-15", "2026-10-15") == "unsettled"
    assert window_category("2026-10-14", "2026-10-14", "2026-10-15") == "settled"
    assert (
        window_category("2025-06-01", "2026-07-02", "2026-10-15", backtrack_days=365)
        == "out_of_range"
    )


def test_normalize_params_set_semantics():
    left = normalize_params(
        "ScjdQuery",
        {"groupbyFields": ["dept", "dh"], "orderbyFields": ["dh"], "Type": "2,1,1"},
        "2026-07-01",
        "2026-07-02",
    )
    right = normalize_params(
        "ScjdQuery",
        {"groupbyFields": ["dh", "dept"], "orderbyFields": ["dept"], "Type": "1,2"},
        "2026-07-01",
        "2026-07-02",
    )
    # 排序不进键（orderbyFields 跳过）、groupby 集合化、Type 排序去重。
    assert left == right
    assert "groupby" not in left
    assert left["Type"] == "1,2"


def test_cacheable_grain():
    assert cacheable_grain("GongziClQuery", {"Flag": "0"})
    assert cacheable_grain("GongziClQuery", {"groupbyFields": ["uid"]})
    assert cacheable_grain("ScjdQuery", {"groupbyFields": M7_GROUPBY})
    assert cacheable_grain("ScjdQuery", {"groupbyFields": ["dh"]})
    assert not cacheable_grain("DeptQuery", {})


def test_result_set_round_trip():
    rows = (
        {"dh": "20260701001", "rq": "2026-07-01", "dept": "D1"},
        {"dh": "20260702001", "rq": "2026-07-02", "dept": "D2"},
    )
    payload = encode_result_set(
        operation_id="ScjdQuery",
        rows=rows,
        footer={"sssl_total": "10"},
        window=PREFETCH_WINDOW,
        filters={"api": "ScjdQuery"},
        derived=(),
        generated_at="2026-10-15T02:00:00+00:00",
        source="precompute",
    )
    decoded = decode_result_set(payload)
    assert decoded is not None
    assert decoded["v"] == RESULT_SET_VERSION
    assert decoded["api"] == "ScjdQuery"
    assert decoded["footer"] == {"sssl_total": "10"}
    assert decoded["decoded_rows"] == [dict(row) for row in rows]


def test_decode_rejects_corrupt_payloads():
    assert decode_result_set(b"not json") is None
    assert decode_result_set(b"[1,2,3]") is None
    bad_version = encode_result_set(
        operation_id="ScjdQuery",
        rows=({"a": "1"},),
        footer=None,
        window=PREFETCH_WINDOW,
        filters={},
        derived=(),
        generated_at="now",
        source="precompute",
    ).replace(f'"v":{RESULT_SET_VERSION}'.encode(), b'"v":0')
    assert decode_result_set(bad_version) is None


def test_footer_reconciles_integer_strict_and_float_tolerance():
    rows = ({"sssl": "2"}, {"sssl": "3"})
    assert footer_reconciles(rows, {"sssl_total": "5"})
    assert not footer_reconciles(rows, {"sssl_total": "6"})
    assert footer_reconciles(rows, {"sssl_total": "5.0"})  # 整值仍按严格相等通过
    floats = ({"je": "50.1"}, {"je": "49.9"})
    assert footer_reconciles(floats, {"je_total": "100.0"}, tolerance=1e-6)
    assert not footer_reconciles(floats, {"je_total": "101.0"}, tolerance=1e-6)
    assert not footer_reconciles(({"sssl": "abc"},), {"sssl_total": "1"})
    assert footer_reconciles(rows, None)


def test_footer_key_whose_column_is_absent_is_skipped_not_failed():
    """footer 声明的列不在行集里 → 本地无从对账，跳过而不是判 mismatch。

    实测 2026-10-11（真实数据）：GongziClQuery 的 ``sssl_total`` = 29305439，
    而 173572 行明细里没有 ``sssl`` 这一列——逐行求和只能是 0。旧实现把它当成
    「对账失败」，于是**每一条预取都被拒写，缓存永远是空的**。
    """
    rows = ({"sl": "29305439"}, {"sl": "0"})

    assert footer_reconciles(rows, {"sssl_total": "29305439", "sl_total": "29305439"})
    # 能对账的键仍然严格判定：列在行集里的不准放过。
    assert not footer_reconciles(rows, {"sssl_total": "1", "sl_total": "29305440"})


def test_non_additive_footer_column_is_skipped():
    """``wcl`` = 包完工率（0~100），footer 给的是整体值而非行值之和 → 不参与对账。

    实测 2026-10-11：ScjdQuery 9001 行的 ``wcl`` 逐行求和 = 898267.11，而
    ``footer.wcl_total`` = 99.77（整体完工率）。
    """
    rows = ({"wcl": "100"}, {"wcl": "99"}, {"wcl": "100"})

    assert footer_reconciles(rows, {"wcl_total": "99.77"})
    mixed = ({"wcl": "100", "bs": "7"},)
    assert footer_reconciles(mixed, {"wcl_total": "99.77", "bs_total": "7"})
    # 比率列被跳过，但绝不能掩盖同一个 footer 里可对账键的真实不一致。
    assert not footer_reconciles(mixed, {"wcl_total": "0", "bs_total": "8"})


def test_shadow_compare_distinguishes_tie_order():
    rows_a: Rows = ({"uid": "U1", "sssl": "5"}, {"uid": "U2", "sssl": "5"})
    rows_b: Rows = ({"uid": "U2", "sssl": "5"}, {"uid": "U1", "sssl": "5"})
    realtime_a = ResourceFetchResult(
        rows=rows_a, total=2, pages_fetched=1, complete=True, footer=None
    )
    realtime_b = ResourceFetchResult(
        rows=rows_b, total=2, pages_fetched=1, complete=True, footer=None
    )
    assert shadow_compare(rows_a, None, realtime_a).consistent is True
    verdict = shadow_compare(rows_a, None, realtime_b)
    assert verdict.consistent is False
    assert verdict.tie_order_only is True
    changed = ResourceFetchResult(
        rows=({"uid": "U1", "sssl": "6"}, {"uid": "U2", "sssl": "5"}),
        total=2,
        pages_fetched=1,
        complete=True,
        footer=None,
    )
    verdict = shadow_compare(rows_a, None, changed)
    assert verdict.consistent is False
    assert verdict.tie_order_only is False
    footer_drift = ResourceFetchResult(
        rows=rows_a, total=2, pages_fetched=1, complete=True, footer={"x": "1"}
    )
    assert shadow_compare(rows_a, None, footer_drift).consistent is False


def test_scope_filtered_rows():
    rows: Rows = (
        {"dept": "D1", "uid": "U1"},
        {"dept": "D2", "uid": "U2"},
        {"dept": "", "uid": ""},  # 进度侧未扫码工序组
    )
    owner = owner_filters()
    assert scope_filtered_rows(rows, "ScjdQuery", owner) == rows
    dept = NarrowedFilters(
        tenant_id=TenantId("t"), employee_ids=None, dept_ids=frozenset([DeptId("D1")])
    )
    # 部门视角：空 dept 行一并排除（附录 A.4）。
    assert scope_filtered_rows(rows, "ScjdQuery", dept) == ({"dept": "D1", "uid": "U1"},)
    employee = NarrowedFilters(
        tenant_id=TenantId("t"),
        employee_ids=frozenset([EmployeeId("U2")]),
        dept_ids=None,
    )
    assert scope_filtered_rows(rows, "ScjdQuery", employee) == ({"dept": "D2", "uid": "U2"},)


def test_time_anchor_column_and_slicing():
    assert time_anchor_column("ScjdQuery", {}) == PROGRESS_DATE_COLUMN
    assert time_anchor_column("GongziClQuery", {"Flag": "1"}) == "check_time"
    assert time_anchor_column("GongziClQuery", {"Flag": "0"}) == "inputdate"
    assert time_anchor_column("GongziClQuery", {}) == "inputdate"
    rows = (
        {"inputdate": "2026-07-01 10:00:00"},
        {"inputdate": "2026-07-02 10:00:00"},
    )
    sliced = time_sliced_rows(rows, "inputdate", ("2026-07-02", "2026-07-02"))
    assert sliced == ({"inputdate": "2026-07-02 10:00:00"},)


# ---------------------------------------------------------------------------
# V2ResultCache 读写


def test_write_and_read_round_trip_payroll():
    cache = make_result_cache()
    rows: Rows = (
        {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "5"},
        {"inputdate": "2026-07-02 10:00:00", "uid": "U2", "sssl": "3"},
    )
    assert cache.mode is ResultCacheMode.READWRITE
    assert (
        asyncio.run(
            cache.write(
                TenantId("t"),
                "GongziClQuery",
                rows,
                None,
                {"Flag": "0", "Type": "0,1,2"},
                ("2026-07-01", "2026-07-02"),
                source="query",
            )
        )
        is True
    )
    hit = asyncio.run(
        cache.read(
            TenantId("t"),
            "GongziClQuery",
            {"Flag": "0", "Type": "0,1,2"},
            ("2026-07-01", "2026-07-02"),
        )
    )
    assert hit is not None
    assert hit.rows == rows
    assert hit.source == "query"


def test_read_uses_prefetch_key_for_settled_question_window():
    cache = make_result_cache()
    rows: Rows = ({"dh": "NON-DATE-SHEET", "rq": "2026-07-01T12:00:00", "dept": "D1"},)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "ScjdQuery",
            rows,
            None,
            {},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    # 问题窗口落在年度范围内，缓存 rq 保存接口提供的标准业务日期。
    hit = asyncio.run(
        cache.read(
            TenantId("t"),
            "ScjdQuery",
            {"groupbyFields": M7_GROUPBY},
            ("2026-07-01", "2026-07-02"),
        )
    )
    assert hit is not None
    assert hit.rows == ({"dh": "NON-DATE-SHEET", "dept": "D1", "rq": "2026-07-01"},)
    assert hit.source == "precompute"


def test_read_miss_out_of_range_uses_question_window_key():
    cache = make_result_cache()
    rows: Rows = ({"inputdate": "2024-01-05 10:00:00"},)
    # 写入的是问题窗口键（超出回溯范围 → 不读预取键）。
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            rows,
            None,
            {},
            ("2024-01-01", "2024-01-31"),
            source="query",
        )
    )
    hit = asyncio.run(cache.read(TenantId("t"), "GongziClQuery", {}, ("2024-01-01", "2024-01-31")))
    assert hit is not None


def test_read_logs_hit_and_miss_with_the_keys_it_probed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """命中/未命中必须各留一行日志——否则 shadow 在日志里与「纯实时」同形。"""
    cache = make_result_cache()
    rows: Rows = ({"inputdate": "2026-07-01 10:00:00"},)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            rows,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    with caplog.at_level(logging.INFO, logger="factory_agent.data_api.result_cache"):
        hit = asyncio.run(
            cache.read(
                TenantId("t"),
                "GongziClQuery",
                {"Flag": "0", "Type": "0,1,2"},
                ("2026-07-01", "2026-07-02"),
            )
        )
        miss = asyncio.run(
            cache.read(TenantId("t"), "GongziClQuery", {"Flag": "9"}, ("2026-07-01", "2026-07-02"))
        )
    assert hit is not None
    assert miss is None

    events = {record.getMessage() for record in caplog.records}
    assert {"v2_result.hit", "v2_result.miss"} <= events

    hit_record = next(r for r in caplog.records if r.getMessage() == "v2_result.hit")
    hit_extra = hit_record.__dict__
    assert hit_extra["operation"] == "GongziClQuery"
    assert hit_extra["rows"] == 1
    assert hit_extra["source"] == "precompute"
    assert len(str(hit_extra["key_digest"])) == 64

    miss_record = next(r for r in caplog.records if r.getMessage() == "v2_result.miss")
    miss_extra = miss_record.__dict__
    assert miss_extra["settled"] is True
    probes = cast("list[str]", miss_extra["key_digests"])
    assert probes
    assert all(len(key) == 64 for key in probes)


def test_today_follows_the_configured_timezone_not_utc():
    """回归：读侧曾用 UTC 算「今天」，与写侧（部署时区）相差一天。"""
    utc_cache = make_result_cache(clock=lambda: LATE_UTC)
    cst_cache = make_result_cache(clock=lambda: LATE_UTC, timezone=CST)
    assert utc_cache._today() == "2026-10-15"  # pyright: ignore[reportPrivateUsage]
    assert cst_cache._today() == "2026-10-16"  # pyright: ignore[reportPrivateUsage]


def test_prefetch_window_today_matches_the_writer_side():
    """写侧 ``run_once`` 与读侧 ``_today()`` 必须得到同一个「今天」。

    差一天 → 读侧探测的预取键写侧从未写过 → 预取 100% 落空（见方案 §4.3）。
    """
    reader = make_result_cache(clock=lambda: LATE_UTC, timezone=CST)
    seen: list[str] = []

    class Recorder:
        def prefetch_window(self, today: str) -> tuple[str, str]:
            seen.append(today)
            return reader.prefetch_window(today)

        async def write(self, *args: Any, **kwargs: Any) -> bool:
            return True

    async def incomplete(
        operation_id: str, window: tuple[str, str], extra_params: Any
    ) -> ResourceFetchResult:
        return ResourceFetchResult(rows=(), total=0, pages_fetched=0, complete=False, reason="test")

    worker = V2PrefetchWorker(
        incomplete,
        Recorder(),  # type: ignore[arg-type]
        tenant_id="t",
        timezone_name="Asia/Shanghai",
        clock=lambda: LATE_UTC,
    )
    asyncio.run(worker.run_once())
    reader_today = reader._today()  # pyright: ignore[reportPrivateUsage]
    assert seen == [reader_today] == ["2026-10-16"]


def test_write_gate_rejects_footer_mismatch():
    cache = make_result_cache()
    rows: Rows = ({"sssl": "2"}, {"sssl": "3"})
    stored = asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            rows,
            {"sssl_total": "999"},
            {},
            ("2026-07-01", "2026-07-02"),
            source="query",
        )
    )
    assert stored is False
    assert (
        asyncio.run(cache.read(TenantId("t"), "GongziClQuery", {}, ("2026-07-01", "2026-07-02")))
        is None
    )


def test_write_rejects_scjd_rows_without_explicit_rq():
    cache = make_result_cache()
    stored = asyncio.run(
        cache.write(
            TenantId("t"),
            "ScjdQuery",
            ({"dh": "202607010001"},),
            None,
            {},
            ("2026-07-01", "2026-07-02"),
            source="query",
        )
    )
    assert stored is False


@pytest.mark.parametrize("rq", [None, "", "2026-02-30", "2026-07-01T99:00:00", "DATE-CANARY"])
def test_bad_rq_cannot_use_date_shaped_sheet_id_or_legacy_anchor_to_enter_cache(rq: object):
    store = MemoryStore()
    cache = make_result_cache(store=store)
    written = asyncio.run(
        cache.write(
            TenantId("t"),
            "ScjdQuery",
            ({"dh": "20260701001", "rq": rq, "dh_date": "2026-07-01"},),
            None,
            {},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    assert written is False
    assert store.data == {}


def test_progress_cache_slicing_uses_rq_even_when_sheet_prefix_disagrees():
    store = MemoryStore()
    cache = make_result_cache(store=store)
    rows: Rows = (
        {"dh": "NON-DATE-SHEET", "rq": "2026-07-01T12:00:00", "fhsl": "10"},
        {"dh": "20260701001", "rq": "2026-08-01", "fhsl": "20"},
    )
    assert asyncio.run(
        cache.write(
            TenantId("t"), "ScjdQuery", rows, None, {}, PREFETCH_WINDOW, source="precompute"
        )
    )
    hit = asyncio.run(cache.read(TenantId("t"), "ScjdQuery", {}, ("2026-07-01", "2026-07-02")))
    assert hit is not None
    sliced = local_view_rows(
        hit.rows, "ScjdQuery", owner_filters(), {}, ("2026-07-01", "2026-07-02"), Role.OWNER
    )
    assert sliced == ({"dh": "NON-DATE-SHEET", "rq": "2026-07-01", "fhsl": "10"},)
    payload = json.loads(next(iter(store.data.values())))
    assert payload["v"] == 3
    assert payload["derived"] == []
    assert "dh_date" not in payload["columns"]


@pytest.mark.parametrize("corruption", ["missing_rq", "bad_rq", "timestamp_rq", "old_version"])
def test_progress_cache_read_rejects_missing_invalid_rq_and_legacy_payload(corruption: str):
    store = MemoryStore()
    cache = make_result_cache(store=store)
    assert asyncio.run(
        cache.write(
            TenantId("t"),
            "ScjdQuery",
            ({"dh": "SHEET-A", "rq": "2026-07-01"},),
            None,
            {},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    key = next(iter(store.data))
    payload = json.loads(store.data[key])
    position = payload["columns"].index("rq")
    if corruption == "missing_rq":
        payload["columns"].pop(position)
        for row in payload["rows"]:
            row.pop(position)
    elif corruption == "bad_rq":
        payload["rows"][0][position] = "2026-02-30"
    elif corruption == "timestamp_rq":
        payload["rows"][0][position] = "2026-07-01T08:00:00+08:00"
    else:
        payload["v"] = 2
    store.data[key] = json.dumps(payload).encode()
    hit = asyncio.run(cache.read(TenantId("t"), "ScjdQuery", {}, ("2026-07-01", "2026-07-02")))
    assert hit is None


def test_ttl_is_window_classified():
    store = MemoryStore()
    cache = make_result_cache(
        policy=ResultCachePolicy(unsettled_ttl_seconds=90, settled_ttl_seconds=86400),
        store=store,
    )
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            ({"inputdate": "2026-07-01 10:00:00"},),
            None,
            {},
            ("2026-07-01", "2026-07-02"),
            source="query",
        )
    )
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            ({"inputdate": "2026-10-15 10:00:00"},),
            None,
            {},
            ("2026-10-15", "2026-10-15"),
            source="query",
        )
    )
    ttls = list(store.ttls.values())
    assert ttls.count(86400) == 1  # settled
    assert ttls.count(90) == 1  # unsettled


# ---------------------------------------------------------------------------
# ResultCacheExecutor


def test_local_view_applies_scope_filter_then_window_slice():
    """本地视图 = 范围过滤 → 时间锚截取；两步的顺序与语义都固定。"""
    filters = NarrowedFilters(
        tenant_id=TenantId("t"), employee_ids=None, dept_ids=frozenset({DeptId("001")})
    )
    rows: Rows = (
        {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "5", "dept": "001"},
        {"inputdate": "2026-06-30 10:00:00", "uid": "U2", "sssl": "3", "dept": "001"},  # 窗口外
        {"inputdate": "2026-07-01 10:00:00", "uid": "U3", "sssl": "7", "dept": "002"},  # 范围外
    )
    view = local_view_rows(
        rows, "GongziClQuery", filters, {"Flag": "0"}, ("2026-07-01", "2026-07-02")
    )
    assert view == (rows[0],)


class InnerFake:
    """记录调用的内层取数替身。"""

    def __init__(self, results: dict[str, ResourceFetchResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, str, str]] = []

    def validate_step(self, filters: Any, request: Any, active_scope: Any = None) -> None:
        pass

    async def execute_full_step(
        self,
        filters: Any,
        request: Any,
        active_scope: Any | None = None,
        extra_params: Any = None,
    ) -> ResourceFetchResult:
        window = request.time_range
        self.calls.append(
            (request.operation_id, window[0].date().isoformat(), window[1].date().isoformat())
        )
        return self._results[request.operation_id]

    async def other_port(self) -> str:  # 供 __getattr__ 透传断言
        return "passed-through"


PAYROLL_ROWS: Rows = (
    {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "5"},
    {"inputdate": "2026-07-02 10:00:00", "uid": "U2", "sssl": "3"},
)
QUESTION_WINDOW = (
    datetime(2026, 7, 1, tzinfo=timezone.utc),
    datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc),
)


def payroll_request(role: Role = Role.OWNER) -> ExecutionRequest:
    return ExecutionRequest(operation_id="GongziClQuery", time_range=QUESTION_WINDOW, role=role)


def make_executor(
    mode: ResultCacheMode,
    inner: InnerFake,
    alerts: Any | None = None,
) -> tuple[ResultCacheExecutor, InnerFake]:
    executor = ResultCacheExecutor(inner, make_result_cache(mode=mode), alerts=alerts)
    return executor, inner


def seeded_executor(
    mode: ResultCacheMode, rows: tuple[dict[str, Any], ...] = PAYROLL_ROWS
) -> tuple[ResultCacheExecutor, InnerFake]:
    cache = make_result_cache(mode=mode)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            rows,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=rows, total=len(rows), pages_fetched=1, complete=True, footer=None
            )
        }
    )
    return ResultCacheExecutor(inner, cache), inner


def test_read_path_serves_cache_without_mes_call():
    executor, inner = seeded_executor(ResultCacheMode.READ)
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert inner.calls == []  # 0 次 MES 调用（§4.5 路径 1）
    # 时间锚截取：问题窗口 [07-01, 07-02] 内两行都在。
    assert result.rows == PAYROLL_ROWS
    assert result.complete is True


def test_read_path_recomputes_footer_for_served_rows():
    """预取键的 footer 是整窗合计；切片后 serve 的 footer 必须与行集自洽。"""
    cache = make_result_cache(mode=ResultCacheMode.READ)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            PAYROLL_ROWS,
            {"sssl_total": "8"},  # 整预取窗口的 MES 合计（5 + 3，过写入门禁）
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True, footer=None
            )
        }
    )
    executor = ResultCacheExecutor(inner, cache)
    request = ExecutionRequest(
        operation_id="GongziClQuery",
        time_range=(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 23, 59, 59, tzinfo=timezone.utc),
        ),
        role=Role.OWNER,
    )
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), request, extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    # 只 serve 07-01 一行（sssl=5）→ footer 本地重算为 5，而非整窗的 8：
    # FR-002/012 的对账不再误报。
    assert result.rows == (PAYROLL_ROWS[0],)
    assert result.footer == {"sssl_total": "5"}


def test_scope_filter_applies_on_read_path():
    executor, _ = seeded_executor(ResultCacheMode.READ)
    filters = NarrowedFilters(
        tenant_id=TenantId("t"), employee_ids=frozenset([EmployeeId("U2")]), dept_ids=None
    )
    result = asyncio.run(
        executor.execute_full_step(
            filters, payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert result.rows == (PAYROLL_ROWS[1],)


def test_write_path_owner_realtime_backfills_cache():
    executor, inner = make_executor(
        ResultCacheMode.READWRITE,
        InnerFake(
            {
                "GongziClQuery": ResourceFetchResult(
                    rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True, footer=None
                )
            }
        ),
    )
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert len(inner.calls) == 1
    assert result.rows == PAYROLL_ROWS
    # 回写后再次请求命中（窗口未含今天 → 24h 回写键）。
    second = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert len(inner.calls) == 1
    assert second.rows == PAYROLL_ROWS


def test_write_skipped_for_non_owner_and_incomplete():
    executor, inner = make_executor(
        ResultCacheMode.READWRITE,
        InnerFake(
            {
                "GongziClQuery": ResourceFetchResult(
                    rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True, footer=None
                )
            }
        ),
    )
    asyncio.run(
        executor.execute_full_step(
            owner_filters(),
            payroll_request(role=Role.MANAGER),
            extra_params={"Flag": "0", "Type": "0,1,2"},
        )
    )
    # 非 OWNER 的实时拉取不是全厂行集，绝不写全厂键。
    miss = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert miss.rows == PAYROLL_ROWS
    assert len(inner.calls) == 2

    incomplete = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=PAYROLL_ROWS,
                total=2,
                pages_fetched=1,
                complete=False,
                reason="total_drift",
                footer=None,
            )
        }
    )
    incomplete_executor, _ = make_executor(ResultCacheMode.READWRITE, incomplete)
    asyncio.run(
        incomplete_executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert len(incomplete.calls) == 1  # 未回写


def test_non_cacheable_grain_passthrough():
    executor, inner = make_executor(
        ResultCacheMode.READWRITE,
        InnerFake(
            {
                "ScjdQuery": ResourceFetchResult(
                    rows=(), total=0, pages_fetched=1, complete=True, footer=None
                )
            }
        ),
    )
    request = ExecutionRequest(
        operation_id="ScjdQuery", time_range=QUESTION_WINDOW, role=Role.OWNER
    )
    asyncio.run(
        executor.execute_full_step(owner_filters(), request, extra_params={"groupbyFields": ["dh"]})
    )
    assert len(inner.calls) == 1


def test_shadow_mode_consistent_returns_cached_rows():
    executor, inner = seeded_executor(ResultCacheMode.SHADOW)
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert len(inner.calls) == 1  # 影子比对仍实时取一次
    assert result.rows == PAYROLL_ROWS  # 返回缓存（截取后仍在窗口内）


def test_final_recipe_mismatch_hook_evicts_its_cached_input():
    alerts: list[tuple[str, dict[str, Any]]] = []

    def record_alert(name: str, detail: dict[str, Any]) -> None:
        alerts.append((name, detail))

    realtime_rows: Rows = (
        {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "9"},
        {"inputdate": "2026-07-02 10:00:00", "uid": "U2", "sssl": "3"},
    )
    cache = make_result_cache(mode=ResultCacheMode.SHADOW)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            PAYROLL_ROWS,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=realtime_rows, total=2, pages_fetched=1, complete=True, footer=None
            )
        }
    )
    executor = ResultCacheExecutor(inner, cache, alerts=record_alert)
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    # 不一致：返回实时结果、驱逐缓存键、告警。
    assert result.rows == PAYROLL_ROWS
    assert result.shadow_result is not None
    assert result.shadow_result.rows == realtime_rows
    asyncio.run(executor.discard_cached_result(result))
    assert [name for name, _ in alerts] == ["cache_shadow_mismatch"]
    miss = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert miss.rows == realtime_rows  # 缓存已被驱逐


#: 2026-08-31 夜班补录：属 8 月，绝不能被 9 月窗口的缓存切片捞进来。
AUGUST_STRAY: Rows = ({"inputdate": "2026-08-31 23:30:00", "uid": "U_AUG", "sssl": "15943.10"},)
SEPTEMBER_ROWS: Rows = (
    {"inputdate": "2026-09-01 09:00:00", "uid": "U_SEP1", "sssl": "100"},
    {"inputdate": "2026-09-30 09:00:00", "uid": "U_SEP30", "sssl": "200"},
    {"inputdate": "2026-10-01 00:30:00", "uid": "U_OCT", "sssl": "300"},
)
#: 9 月（厂区时区）= 半开 UTC ``[09-01T00:00+08:00, 10-01T00:00+08:00)``。
SEPTEMBER_REQUEST = ExecutionRequest(
    operation_id="GongziClQuery",
    time_range=(datetime(2026, 9, 1, tzinfo=CST), datetime(2026, 10, 1, tzinfo=CST)),
    role=Role.OWNER,
)


def test_executor_slices_the_cache_on_local_dates_not_utc_ones():
    """回归：``09-01T00:00+08:00`` 不得退成 ``08-31``。

    线上事故：团长问 9 月工资清单，shadow（实时）269 人 / 265089.63，切到
    readwrite（缓存）后变成 277 人 / 281032.73——多出来的正是 08-31 那一天。
    根因是缓存路径对 UTC 值直接取 ``.date()``，而实时路径已换算到厂区时区。
    """
    store = MemoryStore()
    cache = make_result_cache(mode=ResultCacheMode.READ, store=store, timezone=CST)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            AUGUST_STRAY + SEPTEMBER_ROWS,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=(), total=0, pages_fetched=1, complete=True, footer=None
            )
        }
    )
    executor = ResultCacheExecutor(inner, cache)
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), SEPTEMBER_REQUEST, extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert [row["uid"] for row in result.rows] == ["U_SEP1", "U_SEP30"]
    assert inner.calls == []  # 命中预取键，0 次 MES 调用


#: 不属 07 月窗口的行：让「整段预取行集」比「本问题窗口」大，暴露比对对象错误。
OFF_WINDOW_ROWS: Rows = ({"inputdate": "2026-06-15 10:00:00", "uid": "U_JUN", "sssl": "7"},)


def test_shadow_compares_the_served_slice_not_the_whole_prefetch_window():
    """回归：影子比对的对象是**本次 serve 的切片**，不是整段预取行集。

    ``hit.rows`` 是整段预取窗口（线上 ~17 万行），而实时只是本问题窗口：行数
    永远不等 → ``consistent`` 恒 False → shadow 命中也要重取、每次告警、还去
    驱逐一个从未命中的键。
    """
    store = MemoryStore()
    cache = make_result_cache(mode=ResultCacheMode.SHADOW, store=store)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            PAYROLL_ROWS + OFF_WINDOW_ROWS,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True, footer=None
            )
        }
    )
    alerts: list[tuple[str, dict[str, Any]]] = []
    executor = ResultCacheExecutor(
        inner, cache, alerts=lambda name, detail: alerts.append((name, detail))
    )
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert len(inner.calls) == 1  # shadow 仍实时复核一次
    assert result.rows == PAYROLL_ROWS  # 切片与实时逐行一致
    assert alerts == []  # 一致 → 不告警
    assert store.data  # 且不该误驱逐命中的预取键


def test_shadow_mismatch_evicts_the_key_that_was_actually_hit():
    """回归：驱逐必须打在**命中的预取键**上，不是本问题窗口的键（后者从未写过）。

    打在问题窗口上等于空转：坏条目留在 Redis 里，下一次同样的提问继续命中它。
    """
    store = MemoryStore()
    cache = make_result_cache(mode=ResultCacheMode.SHADOW, store=store)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            PAYROLL_ROWS,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    realtime_rows: Rows = (
        {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "9"},
        {"inputdate": "2026-07-02 10:00:00", "uid": "U2", "sssl": "3"},
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=realtime_rows, total=2, pages_fetched=1, complete=True, footer=None
            )
        }
    )
    executor = ResultCacheExecutor(inner, cache)
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    asyncio.run(executor.discard_cached_result(result))
    assert store.data == {}  # 命中键被真正驱逐，而不是驱一个不存在的问题窗口键


#: 带 dept 的底料：命中侧 serve 会按范围过滤，实时侧拿到的却是全厂原始行集。
DEPT_ROWS: Rows = (
    {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "5", "dept": "001"},
    {"inputdate": "2026-07-02 10:00:00", "uid": "U2", "sssl": "3", "dept": "001"},
)
OTHER_DEPT_ROWS: Rows = (
    {"inputdate": "2026-07-01 11:00:00", "uid": "U9", "sssl": "9", "dept": "002"},
)


def test_shadow_supplies_native_realtime_input_for_final_recipe_scope_filtering():
    """回归：实时行集必须过**与命中路径相同**的本地变换，否则永远比不出「一致」。

    线上实测（9 月工资清单）：MES **不做部门过滤**，原始行集 13928 行 / 309 人；
    而命中侧 serve 出去的是过滤后的 9173 行 / 269 人。不归一化就恒判 mismatch →
    命中也被丢弃、每次告警、还驱逐一个健康的好键。
    """
    cache = make_result_cache(mode=ResultCacheMode.SHADOW)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            DEPT_ROWS + OTHER_DEPT_ROWS,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    # 实时返回 MES 原始行集：本部门 + 其它部门。
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=DEPT_ROWS + OTHER_DEPT_ROWS,
                total=3,
                pages_fetched=1,
                complete=True,
                footer=None,
            )
        }
    )
    alerts: list[tuple[str, dict[str, Any]]] = []
    executor = ResultCacheExecutor(
        inner, cache, alerts=lambda name, detail: alerts.append((name, detail))
    )
    filters = NarrowedFilters(
        tenant_id=TenantId("t"), employee_ids=None, dept_ids=frozenset({DeptId("001")})
    )
    result = asyncio.run(
        executor.execute_full_step(
            filters, payroll_request(role=Role.MANAGER), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert alerts == []  # 归一化后两侧一致 → 不告警
    assert result.rows == DEPT_ROWS  # 且返回的是缓存切片


def test_shadow_tie_order_only_drift_still_serves_the_cache():
    """并列项行序漂移不是差异，必须照常 serve 缓存、且不驱逐。

    线上实测：命中的 9173 行与实时 9173 行**多重集全等、footer 全等**，只是并列
    行的先后不同（``tie_order_only=True``）。排序从不下发 MES、一律本地做（F.3），
    取数层行序不在契约内；若判为不一致，shadow 会把每一个健康键都驱逐掉。
    """
    tied: Rows = (
        {"inputdate": "2026-07-01 10:00:00", "uid": "U1", "sssl": "5"},
        {"inputdate": "2026-07-01 10:00:00", "uid": "U2", "sssl": "5"},
    )
    store = MemoryStore()
    cache = make_result_cache(mode=ResultCacheMode.SHADOW, store=store)
    asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            tied,
            None,
            {"Flag": "0", "Type": "0,1,2"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=(tied[1], tied[0]),  # 同样的两行，并列项顺序相反
                total=2,
                pages_fetched=1,
                complete=True,
                footer=None,
            )
        }
    )
    alerts: list[tuple[str, dict[str, Any]]] = []
    executor = ResultCacheExecutor(
        inner, cache, alerts=lambda name, detail: alerts.append((name, detail))
    )
    result = asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert alerts == []  # 数值全等 → 不告警
    assert result.rows == tied  # 照常返回缓存切片
    assert store.data  # 且没有误驱逐健康键


def test_getattr_passes_other_ports_through():
    executor, _ = seeded_executor(ResultCacheMode.READ)
    assert asyncio.run(executor.other_port()) == "passed-through"


def test_owner_bound_department_does_not_restrict_factory_cache_view():
    rows: Rows = (
        {"inputdate": "2026-07-01", "uid": "U1", "dept": "D1", "sl": "5"},
        {"inputdate": "2026-07-01", "uid": "U2", "dept": "D2", "sl": "3"},
    )
    executor, _ = seeded_executor(ResultCacheMode.READ, rows)
    filters = NarrowedFilters(
        tenant_id=TenantId("t"), employee_ids=None, dept_ids=frozenset({DeptId("D1")})
    )
    result = asyncio.run(
        executor.execute_full_step(
            filters, payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert result.rows == rows


def test_owner_employee_subset_cannot_backfill_shared_detail_cache():
    executor, inner = make_executor(
        ResultCacheMode.READWRITE,
        InnerFake(
            {
                "GongziClQuery": ResourceFetchResult(
                    rows=(PAYROLL_ROWS[0],), total=1, pages_fetched=1, complete=True
                )
            }
        ),
    )
    filters = NarrowedFilters(
        tenant_id=TenantId("t"), employee_ids=frozenset({EmployeeId("U1")}), dept_ids=None
    )
    asyncio.run(
        executor.execute_full_step(
            filters, payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "0", "Type": "0,1,2"}
        )
    )
    assert len(inner.calls) == 2


class SnapshotFake:
    def __init__(self, result: ResourceFetchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str, tuple[str, str], dict[str, Any]]] = []

    async def __call__(
        self, tenant_id: Any, operation_id: str, window: tuple[str, str], extra_params: Any
    ) -> ResourceFetchResult:
        self.calls.append((str(tenant_id), operation_id, window, dict(extra_params)))
        return self.result


def progress_detail_rows() -> Rows:
    return (
        {
            "dh": "20260701001",
            "rq": "2026-07-01",
            "jhdh": "P1",
            "chuanghao": "C1",
            "huohao": "H1",
            "bbreed": "S1",
            "dept": "D1",
            "deptname": "Group 1",
            "uid": "U1",
            "uname": "Worker 1",
            "baohao": "1",
            "userid": "M1",
            "color": "Blue",
            "chima": "M",
            "worktype": "W1",
            "wtname": "Step 1",
            "wsort": "1",
            "bs": "1",
            "fhsl": "10",
            "sssl": "5",
            "zpsl": "0",
            "wgs": "1",
            "wts": "1",
        },
        {
            "dh": "20260702002",
            "rq": "2026-07-02",
            "jhdh": "P2",
            "chuanghao": "C2",
            "huohao": "H2",
            "bbreed": "S2",
            "dept": "D2",
            "deptname": "Group 2",
            "uid": "U2",
            "uname": "Worker 2",
            "baohao": "2",
            "userid": "M2",
            "color": "Red",
            "chima": "L",
            "worktype": "W2",
            "wtname": "Step 2",
            "wsort": "2",
            "bs": "1",
            "fhsl": "20",
            "sssl": "20",
            "zpsl": "0",
            "wgs": "1",
            "wts": "1",
        },
    )


def test_cold_manager_query_fills_complete_owner_snapshot_and_reuses_it():
    rows: Rows = (
        {"inputdate": "2026-07-01", "uid": "U1", "dept": "D1", "sl": "5"},
        {"inputdate": "2026-07-01", "uid": "U2", "dept": "D2", "sl": "3"},
    )
    snapshot = SnapshotFake(ResourceFetchResult(rows=rows, total=2, pages_fetched=1, complete=True))
    cache = make_result_cache()
    inner = InnerFake({})
    executor = ResultCacheExecutor(inner, cache, snapshot_provider=snapshot)
    filters = replace(owner_filters(), dept_ids=frozenset({DeptId("D1")}))
    first = asyncio.run(executor.execute_full_step(filters, payroll_request(Role.MANAGER)))
    second = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert first.rows == (rows[0],)
    assert second.rows == rows
    assert len(snapshot.calls) == 1
    assert inner.calls == []
    assert second.cache_source == "query"
    assert second.cache_generated_at == TODAY.isoformat()


def test_current_window_employee_repeat_uses_short_ttl_owner_snapshot():
    store = MemoryStore()
    cache = make_result_cache(store=store)
    rows: Rows = (
        {"inputdate": "2026-10-15", "uid": "U1", "dept": "D2", "sl": "5"},
        {"inputdate": "2026-10-15", "uid": "U2", "dept": "D2", "sl": "7"},
    )
    snapshot = SnapshotFake(ResourceFetchResult(rows=rows, total=2, pages_fetched=1, complete=True))
    inner = InnerFake({})
    executor = ResultCacheExecutor(inner, cache, snapshot_provider=snapshot)
    filters = replace(owner_filters(), employee_ids=frozenset({EmployeeId("U1")}))
    request = ExecutionRequest(
        "GongziClQuery",
        (datetime(2026, 10, 15, tzinfo=timezone.utc), datetime(2026, 10, 16, tzinfo=timezone.utc)),
        role=Role.EMPLOYEE,
    )
    first = asyncio.run(executor.execute_full_step(filters, request))
    second = asyncio.run(executor.execute_full_step(filters, request))
    assert first.rows == second.rows == (rows[0],)
    assert len(snapshot.calls) == 1
    assert inner.calls == []
    assert set(store.ttls.values()) == {90}


def test_proven_empty_snapshot_is_cached():
    snapshot = SnapshotFake(ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True))
    executor = ResultCacheExecutor(InnerFake({}), make_result_cache(), snapshot_provider=snapshot)
    for _ in range(2):
        result = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
        assert result.complete
        assert result.rows == ()
    assert len(snapshot.calls) == 1


def test_incomplete_snapshot_falls_back_without_cache_write():
    incomplete = ResourceFetchResult(rows=PAYROLL_ROWS, total=3, pages_fetched=1, complete=False)
    snapshot = SnapshotFake(incomplete)
    inner = InnerFake({"GongziClQuery": incomplete})
    store = MemoryStore()
    executor = ResultCacheExecutor(
        inner, make_result_cache(store=store), snapshot_provider=snapshot
    )
    result = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert not result.complete
    assert result.cache_source is None
    assert len(inner.calls) == len(snapshot.calls) == 1
    assert store.data == {}


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("order_codes", frozenset({"P1"})),
        ("style_codes", frozenset({"S1"})),
        ("plan_codes", frozenset({"P1"})),
        ("material_ids", frozenset({"M1"})),
    ],
)
def test_business_filtered_owner_response_does_not_pollute_shared_key(attribute: str, value: Any):
    rows = progress_detail_rows()
    inner = InnerFake(
        {"ScjdQuery": ResourceFetchResult(rows=(rows[0],), total=1, pages_fetched=1, complete=True)}
    )
    store = MemoryStore()
    executor = ResultCacheExecutor(inner, make_result_cache(store=store))
    request = replace(payroll_request(), operation_id="ScjdQuery")
    filters = replace(owner_filters(), **{attribute: value})
    asyncio.run(executor.execute_full_step(filters, request))
    assert store.data == {}


def test_progress_views_reuse_one_detail_snapshot_after_complete_filtering():
    rows = progress_detail_rows()
    snapshot = SnapshotFake(ResourceFetchResult(rows=rows, total=2, pages_fetched=1, complete=True))
    inner = InnerFake({})
    executor = ResultCacheExecutor(inner, make_result_cache(), snapshot_provider=snapshot)
    request = replace(payroll_request(), operation_id="ScjdQuery")
    listed = asyncio.run(
        executor.execute_full_step(
            owner_filters(), request, extra_params={"groupbyFields": M7_GROUPBY}
        )
    )
    package = asyncio.run(
        executor.execute_full_step(
            replace(owner_filters(), order_codes=frozenset({"20260701001"})),
            request,
            extra_params={"groupbyFields": ["dh", "baohao", "userid", "color", "chima"]},
        )
    )
    process = asyncio.run(
        executor.execute_full_step(
            replace(owner_filters(), material_ids=frozenset({"M1"})),
            request,
            extra_params={
                "groupbyFields": [
                    "userid",
                    "uid",
                    "uname",
                    "dept",
                    "deptname",
                    "worktype",
                    "wtname",
                    "wsort",
                ]
            },
        )
    )
    assert len(listed.rows) == 2
    assert package.rows[0]["userid"] == process.rows[0]["userid"] == "M1"
    assert package.rows[0]["wcl"] == process.rows[0]["wcl"] == "50.00"
    assert len(package.rows) == len(process.rows) == 1
    assert len(snapshot.calls) == 1
    assert snapshot.calls[0][3] == {}
    assert inner.calls == []


def test_progress_shadow_preserves_native_rq_rows_without_false_mismatch():
    rows = progress_detail_rows()
    store = MemoryStore()
    cache = make_result_cache(mode=ResultCacheMode.SHADOW, store=store)
    assert asyncio.run(
        cache.write(
            TenantId("t"), "ScjdQuery", rows, None, {}, PREFETCH_WINDOW, source="precompute"
        )
    )
    realtime = ResourceFetchResult(rows=rows, total=2, pages_fetched=1, complete=True)
    inner = InnerFake({"ScjdQuery": realtime})
    alerts: list[tuple[str, dict[str, Any]]] = []
    executor = ResultCacheExecutor(
        inner, cache, alerts=lambda name, detail: alerts.append((name, detail))
    )
    request = replace(payroll_request(), operation_id="ScjdQuery")
    result = asyncio.run(executor.execute_full_step(owner_filters(), request))
    assert all(row["rq"] for row in result.rows)
    assert result.shadow_result is realtime
    assert alerts == []
    executor.release_cached_result(result)
    assert store.data


def test_grouped_m7_and_old_structure_cannot_enter_detail_cache():
    store = MemoryStore()
    cache = make_result_cache(store=store)
    assert not asyncio.run(
        cache.write(
            TenantId("t"),
            "ScjdQuery",
            progress_detail_rows(),
            None,
            {"groupbyFields": M7_GROUPBY},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    assert store.data == {}
    assert asyncio.run(
        cache.write(
            TenantId("t"),
            "ScjdQuery",
            progress_detail_rows(),
            None,
            {},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    for key, value in store.data.items():
        payload = json.loads(value)
        payload["v"] = 1
        store.data[key] = json.dumps(payload).encode()
    assert (
        asyncio.run(cache.read(TenantId("t"), "ScjdQuery", {}, ("2026-07-01", "2026-07-02")))
        is None
    )


def test_cache_permission_guard_runs_before_lookup_or_snapshot_provider():
    class DeniedInner(InnerFake):
        def validate_step(self, filters: Any, request: Any, active_scope: Any = None) -> None:
            raise ForbiddenError()

    inner = DeniedInner({})
    snapshot = SnapshotFake(
        ResourceFetchResult(rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True)
    )
    executor = ResultCacheExecutor(inner, make_result_cache(), snapshot_provider=snapshot)
    with pytest.raises(ForbiddenError):
        asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert snapshot.calls == inner.calls == []


def test_scope_or_credential_extra_parameters_are_rejected_without_business_calls():
    snapshot = SnapshotFake(ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True))
    inner = InnerFake({})
    executor = ResultCacheExecutor(inner, make_result_cache(), snapshot_provider=snapshot)
    with pytest.raises(InvalidRequestError, match="scope and credential"):
        asyncio.run(
            executor.execute_full_step(
                owner_filters(), payroll_request(), extra_params={"uid": "outside-scope-canary"}
            )
        )
    assert snapshot.calls == inner.calls == []


def test_credential_canaries_never_enter_cache_payload_or_logs(caplog: pytest.LogCaptureFixture):
    store = MemoryStore()
    cache = make_result_cache(store=store)
    credential_canary = "credential-value-canary"
    with caplog.at_level(logging.INFO):
        assert not asyncio.run(
            cache.write(
                TenantId("tenant-appkey-canary"),
                "GongziClQuery",
                ({"inputdate": "2026-07-01", "accessToken": credential_canary},),
                None,
                {},
                PREFETCH_WINDOW,
                source="query",
            )
        )
        asyncio.run(
            cache.read(
                TenantId("tenant-appkey-canary"), "GongziClQuery", {}, ("2026-07-01", "2026-07-02")
            )
        )
    captured = repr([record.__dict__ for record in caplog.records])
    assert credential_canary not in captured
    assert "tenant-appkey-canary" not in captured
    assert store.data == {}


@pytest.mark.parametrize("field", ("accessToken", "app_key", "sign_total", "movepassword_total"))
def test_credential_footer_is_rejected_before_storage_and_when_decoding(
    field: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = MemoryStore()
    cache = make_result_cache(store=store)
    canary = "synthetic-footer-private-canary"
    footer = {field: canary}
    with caplog.at_level(logging.INFO):
        written = asyncio.run(
            cache.write(
                TenantId("synthetic-tenant"),
                "GongziClQuery",
                PAYROLL_ROWS,
                footer,
                {},
                PREFETCH_WINDOW,
                source="precompute",
            )
        )
    assert not written
    assert store.data == {}
    assert canary not in caplog.text
    raw = encode_result_set(
        operation_id="GongziClQuery",
        rows=PAYROLL_ROWS,
        footer=footer,
        window=PREFETCH_WINDOW,
        filters={},
        derived=(),
        generated_at=TODAY.isoformat(),
        source="precompute",
    )
    assert decode_result_set(raw) is None


def test_direct_snapshot_write_rejects_credential_parameters() -> None:
    store = MemoryStore()
    written = asyncio.run(
        make_result_cache(store=store).write(
            TenantId("synthetic-tenant"),
            "GongziClQuery",
            PAYROLL_ROWS,
            None,
            {"sign": "synthetic-parameter-private-canary"},
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    assert not written
    assert store.data == {}


def test_ratio_footer_is_recomputed_and_validated_from_contributions():
    rows: Rows = ({"wcl": "0", "sssl": "1", "fhsl": "3"}, {"wcl": "100", "sssl": "2", "fhsl": "3"})
    assert recomputed_footer(rows, {"wcl_total": "0"}) == {"wcl_total": "50.00"}
    assert footer_reconciles(rows, {"wcl_total": "50"})
    assert not footer_reconciles(rows, {"wcl_total": "66.67"})
    assert not footer_reconciles((), {"sl_total": "1"})


@pytest.mark.parametrize(
    "filters",
    [
        NarrowedFilters(TenantId("other-tenant-canary"), None, None),
        NarrowedFilters(TenantId("t"), frozenset({EmployeeId("outside-scope-canary")}), None),
        NarrowedFilters(TenantId("t"), None, frozenset({DeptId("outside-dept-canary")})),
    ],
)
def test_real_scoped_executor_blocks_cache_access_and_owner_snapshot(filters: NarrowedFilters):
    store = MemoryStore()
    cache = make_result_cache(store=store)
    adapter = RecordingAdapter()
    inner = ScopedExecutor(adapter, load_catalog())
    snapshot = SnapshotFake(ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True))
    executor = ResultCacheExecutor(cast(Any, inner), cache, snapshot_provider=snapshot)
    scope = DataScope(
        TenantId("t"),
        frozenset({EmployeeId("U1")}),
        frozenset({DeptId("D1")}),
        TODAY,
        ScopeVersion("test-scope"),
    )
    with pytest.raises(ForbiddenError):
        asyncio.run(
            executor.execute_full_step(filters, payroll_request(Role.MANAGER), active_scope=scope)
        )
    assert store.lookups == []
    assert snapshot.calls == []
    assert adapter.requests == []


def test_disabled_catalog_operation_cannot_be_served_from_cache():
    class DisabledCatalog:
        def get(self, operation_id: str) -> Any:
            return load_catalog().get(operation_id).model_copy(update={"enabled": False})

    store = MemoryStore()
    cache = make_result_cache(store=store)
    adapter = RecordingAdapter()
    executor = ResultCacheExecutor(cast(Any, ScopedExecutor(adapter, DisabledCatalog())), cache)
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert store.lookups == []
    assert adapter.requests == []


def test_cache_storage_failure_preserves_complete_realtime_response():
    class FailingStore(MemoryStore):
        async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
            raise RuntimeError("storage-failure-canary")

    realtime = ResourceFetchResult(rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True)
    inner = InnerFake({"GongziClQuery": realtime})
    alerts: list[tuple[str, dict[str, Any]]] = []
    executor = ResultCacheExecutor(
        inner,
        make_result_cache(store=FailingStore()),
        alerts=lambda name, detail: alerts.append((name, detail)),
    )
    result = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert result is realtime
    assert len(inner.calls) == 1
    assert len(alerts) == 1
    assert alerts[0][0] == "cache_write_failed"
    assert alerts[0][1]["source"] == "caller"
    assert "storage-failure-canary" not in repr(alerts)


def test_corrupt_footer_payload_falls_back_to_source():
    store = MemoryStore()
    cache = make_result_cache(store=store)
    assert asyncio.run(
        cache.write(
            TenantId("t"), "GongziClQuery", PAYROLL_ROWS, None, {}, PREFETCH_WINDOW, source="query"
        )
    )
    for key, value in store.data.items():
        payload = json.loads(value)
        payload["footer"] = "corrupt-footer-canary"
        store.data[key] = json.dumps(payload).encode()
    realtime = ResourceFetchResult(rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True)
    inner = InnerFake({"GongziClQuery": realtime})
    result = asyncio.run(
        ResultCacheExecutor(inner, cache).execute_full_step(owner_filters(), payroll_request())
    )
    assert result is realtime
    assert len(inner.calls) == 1


def test_cache_miss_concurrent_requests_share_one_complete_snapshot():
    class SlowSnapshot(SnapshotFake):
        async def __call__(
            self, tenant_id: Any, operation_id: str, window: tuple[str, str], extra_params: Any
        ) -> ResourceFetchResult:
            await asyncio.sleep(0)
            return await super().__call__(tenant_id, operation_id, window, extra_params)

    snapshot = SlowSnapshot(
        ResourceFetchResult(rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True)
    )
    inner = InnerFake({})
    executor = ResultCacheExecutor(inner, make_result_cache(), snapshot_provider=snapshot)

    async def run_pair() -> list[ResourceFetchResult]:
        return await asyncio.gather(
            *[executor.execute_full_step(owner_filters(), payroll_request()) for _ in range(2)]
        )

    first, second = asyncio.run(run_pair())
    assert first.rows == second.rows == PAYROLL_ROWS
    assert len(snapshot.calls) == 1
    assert inner.calls == []


def test_wage_flag_uses_its_own_anchor_and_type_partitions_snapshot_keys():
    rows: Rows = ({"inputdate": "2026-06-01", "check_time": "2026-07-01", "uid": "U1"},)
    snapshot = SnapshotFake(ResourceFetchResult(rows=rows, total=1, pages_fetched=1, complete=True))
    executor = ResultCacheExecutor(InnerFake({}), make_result_cache(), snapshot_provider=snapshot)
    params = {"Flag": "1", "Type": "0,1,2"}
    first = asyncio.run(
        executor.execute_full_step(owner_filters(), payroll_request(), extra_params=params)
    )
    second = asyncio.run(
        executor.execute_full_step(owner_filters(), payroll_request(), extra_params=params)
    )
    assert first.rows == second.rows == rows
    asyncio.run(
        executor.execute_full_step(
            owner_filters(), payroll_request(), extra_params={"Flag": "1", "Type": "0"}
        )
    )
    assert len(snapshot.calls) == 2
    assert snapshot.calls[0][3] == params


def test_missing_wage_anchor_is_not_cached_as_a_false_empty_result():
    invalid = ResourceFetchResult(rows=({"uid": "U1"},), total=1, pages_fetched=1, complete=True)
    snapshot = SnapshotFake(invalid)
    inner = InnerFake({"GongziClQuery": invalid})
    store = MemoryStore()
    executor = ResultCacheExecutor(
        inner, make_result_cache(store=store), snapshot_provider=snapshot
    )
    result = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert result.rows == ()
    assert result.complete is False
    assert result.reason == "upstream_invalid"
    assert result.source_call_count == 2
    assert result.footer is None
    assert len(inner.calls) == 1
    assert store.data == {}


@pytest.mark.parametrize(
    "operation,style,expected",
    [
        ("GongziClQuery", "H1", True),
        ("GongziClQuery", "S1", True),
        ("ScjdQuery", "H1", False),
        ("ScjdQuery", "S1", True),
    ],
)
def test_style_code_and_name_filter_matches_each_family_contract(
    operation: str, style: str, expected: bool
):
    rows: Rows = (
        {
            "inputdate": "2026-07-01",
            "dh": "20260701001",
            "rq": "2026-07-01",
            "huohao": "H1",
            "bbreed": "S1",
        },
    )
    view = local_view_rows(
        rows,
        operation,
        replace(owner_filters(), style_codes=frozenset({style})),
        {},
        ("2026-07-01", "2026-07-02"),
        Role.OWNER,
    )
    assert view == (rows if expected else ())


@pytest.mark.parametrize(
    "mode,cold_calls,hot_calls",
    [
        (ResultCacheMode.READWRITE, 1, 0),
        (ResultCacheMode.SHADOW, 2, 1),
    ],
)
def test_fresh_snapshot_and_shadow_call_counts_are_not_persisted_to_hot_cache(
    mode: ResultCacheMode, cold_calls: int, hot_calls: int
):
    store = MemoryStore()
    realtime = ResourceFetchResult(rows=PAYROLL_ROWS, total=2, pages_fetched=1, complete=True)
    snapshot = SnapshotFake(realtime)
    inner = InnerFake({"GongziClQuery": realtime})
    executor = ResultCacheExecutor(
        inner, make_result_cache(mode=mode, store=store), snapshot_provider=snapshot
    )
    first = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    second = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert first.source_call_count == cold_calls
    assert second.source_call_count == hot_calls
    assert len(snapshot.calls) == 1
    assert len(inner.calls) == (2 if mode is ResultCacheMode.SHADOW else 0)
    assert all("source_call_count" not in json.loads(value) for value in store.data.values())


@pytest.mark.parametrize("rows", [PAYROLL_ROWS, ()])
def test_snapshot_write_failure_alerts_safely_and_preserves_caller_fallback(rows: Rows):
    class FailingStore(MemoryStore):
        async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
            raise RuntimeError("write-failure-secret-canary")

    realtime = ResourceFetchResult(rows=rows, total=len(rows), pages_fetched=1, complete=True)
    snapshot = SnapshotFake(realtime)
    inner = InnerFake({"GongziClQuery": realtime})
    alerts: list[tuple[str, dict[str, Any]]] = []
    executor = ResultCacheExecutor(
        inner,
        make_result_cache(store=FailingStore()),
        snapshot_provider=snapshot,
        alerts=lambda name, detail: alerts.append((name, detail)),
    )
    result = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request(Role.MANAGER)))
    assert result.rows == rows
    assert result.complete
    assert result.source_call_count == 2
    assert len(inner.calls) == 1
    assert len(alerts) == 1
    assert alerts[0][0] == "cache_write_failed"
    assert alerts[0][1]["rows"] == len(rows)
    assert "write-failure-secret-canary" not in repr(alerts)
    assert "tenant_id" not in alerts[0][1]


def test_snapshot_logical_fetch_count_does_not_count_http_pages():
    snapshot = SnapshotFake(
        ResourceFetchResult(rows=PAYROLL_ROWS, total=2, pages_fetched=3, complete=True)
    )
    executor = ResultCacheExecutor(InnerFake({}), make_result_cache(), snapshot_provider=snapshot)
    result = asyncio.run(executor.execute_full_step(owner_filters(), payroll_request()))
    assert result.source_call_count == 1


def test_encoding_that_crosses_deadline_cannot_publish_a_snapshot(monkeypatch: pytest.MonkeyPatch):
    store = MemoryStore()
    elapsed = [0.5]
    cache = V2ResultCache(
        make_cache(store), clock=lambda: TODAY, monotonic_clock=lambda: elapsed[0]
    )
    original = result_cache_module.encode_result_set

    def encoding(*args: Any, **kwargs: Any) -> bytes:
        payload = original(*args, **kwargs)
        elapsed[0] = 2.0
        return payload

    monkeypatch.setattr(result_cache_module, "encode_result_set", encoding)
    with pytest.raises(TimeoutError, match="publication deadline"):
        asyncio.run(
            cache.write(
                TenantId("t"),
                "GongziClQuery",
                PAYROLL_ROWS,
                None,
                {},
                PREFETCH_WINDOW,
                source="precompute",
                deadline=1.0,
            )
        )
    assert store.data == {}


@pytest.mark.parametrize(
    "case,question_day,expected_amount,expected_calls,expected_probes",
    [
        ("covered_previous_day", "2026-07-01", "5", 0, 2),
        ("new_yesterday", "2026-10-15", "7", 1, 2),
        ("current_day_preferred", "2026-07-01", "9", 0, 1),
    ],
)
def test_cross_midnight_reads_only_one_complete_covering_snapshot(
    case: str,
    question_day: str,
    expected_amount: str,
    expected_calls: int,
    expected_probes: int,
):
    store = MemoryStore()
    clock = [TODAY]
    cache = make_result_cache(mode=ResultCacheMode.READ, store=store, clock=lambda: clock[0])
    params = {"Flag": "0", "Type": "0,1,2"}
    previous_rows: Rows = ({"inputdate": "2026-07-01", "uid": "U1", "sssl": "5"},)
    assert asyncio.run(
        cache.write(
            TenantId("t"),
            "GongziClQuery",
            previous_rows,
            None,
            params,
            PREFETCH_WINDOW,
            source="precompute",
        )
    )
    clock[0] = datetime(2026, 10, 16, 0, 1, tzinfo=timezone.utc)
    if case == "current_day_preferred":
        assert asyncio.run(
            cache.write(
                TenantId("t"),
                "GongziClQuery",
                ({"inputdate": "2026-07-01", "uid": "U1", "sssl": "9"},),
                None,
                params,
                cache.prefetch_window("2026-10-16"),
                source="precompute",
            )
        )
    live_rows: Rows = ({"inputdate": question_day, "uid": "U1", "sssl": "7"},)
    inner = InnerFake(
        {
            "GongziClQuery": ResourceFetchResult(
                rows=live_rows, total=1, pages_fetched=1, complete=True
            )
        }
    )
    question_time = datetime.fromisoformat(question_day).replace(tzinfo=timezone.utc)
    request = ExecutionRequest(
        operation_id="GongziClQuery",
        time_range=(question_time, question_time.replace(hour=23, minute=59, second=59)),
        role=Role.OWNER,
    )
    fetched = asyncio.run(
        ResultCacheExecutor(inner, cache).execute_full_step(
            owner_filters(), request, extra_params=params
        )
    )
    assert fetched.complete
    assert fetched.rows[0]["sssl"] == expected_amount
    assert len(inner.calls) == expected_calls
    assert len(store.lookups) == expected_probes
    if expected_calls == 0:
        assert fetched.source_call_count == 0
        assert fetched.cache_source == "precompute"
    else:
        assert fetched is inner._results["GongziClQuery"]  # pyright: ignore[reportPrivateUsage]
