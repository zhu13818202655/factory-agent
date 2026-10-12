"""V2 夜间预取 worker 单元测试：调度时点、抖动、单轮执行与失败隔离。

对应 ``src/factory_agent/application/prefetch.py`` 与方案 §4.3。
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from factory_agent.application.prefetch import PREFETCH_PLANS, V2PrefetchWorker
from factory_agent.data_api.result_cache import V2ResultCache
from factory_agent.ports.contracts import ResourceFetchResult

CST = ZoneInfo("Asia/Shanghai")


def assert_wall_clock(run_at: datetime, expected: str) -> None:
    """断言目标时区的墙上时钟（如 ``2026-10-16 02:00``）。"""
    assert run_at.astimezone(CST).strftime("%Y-%m-%d %H:%M") == expected


class AlertRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def alert(self, kind: str, detail: dict[str, Any]) -> None:
        self.calls.append((kind, detail))


class FakeResultCache:
    """记录 write 调用的缓存替身（worker 只用 prefetch_window + write）。"""

    def __init__(self) -> None:
        self.writes: list[tuple[str, tuple[str, str], str]] = []
        self.window: tuple[str, str] = ("2025-10-16", "2026-10-15")

    def prefetch_window(self, today: str) -> tuple[str, str]:
        return self.window

    async def write(
        self,
        tenant_id: Any,
        operation_id: str,
        rows: tuple[Mapping[str, Any], ...],
        footer: Mapping[str, str] | None,
        extra_params: Mapping[str, Any],
        window: tuple[str, str],
        *,
        source: str,
    ) -> bool:
        self.writes.append((operation_id, (window[0], window[1]), source))
        return bool(rows)


async def _noop_fetch(
    operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
) -> ResourceFetchResult:
    raise AssertionError(f"fetch should not be called: {operation_id}")


def make_worker(
    clock: Any,
    fetch: Any = _noop_fetch,
    cache: Any = None,
    alerts: AlertRecorder | None = None,
    jitter_seconds: int = 0,
) -> V2PrefetchWorker:
    return V2PrefetchWorker(
        fetch,
        cast("V2ResultCache", cache or FakeResultCache()),
        tenant_id="tenant-1",
        prefetch_hour=2,
        jitter_seconds=jitter_seconds,
        poll_seconds=300,
        timezone_name="Asia/Shanghai",
        clock=clock,
        alerts=alerts,
    )


# ---------------------------------------------------------------------------
# 调度


def test_next_run_at_before_two_am_is_same_day():
    # 01:00 CST = 2026-10-15 17:00 UTC（前一天）。
    now = datetime(2026, 10, 15, 17, 0, 0, tzinfo=timezone.utc)
    worker = make_worker(lambda: now)
    assert_wall_clock(worker.next_run_at(now), "2026-10-16 02:00")


def test_next_run_at_after_two_am_is_next_day():
    # 03:00 CST = 2026-10-14 19:00 UTC → 下一次 = 10-16 02:00 CST。
    now = datetime(2026, 10, 14, 19, 0, 0, tzinfo=timezone.utc)
    worker = make_worker(lambda: now)
    assert_wall_clock(worker.next_run_at(now), "2026-10-16 02:00")


def test_jitter_is_stable_per_tenant():
    now = datetime(2026, 10, 15, 17, 0, 0, tzinfo=timezone.utc)
    worker = make_worker(lambda: now, jitter_seconds=600)
    first = worker.next_run_at(now)
    second = worker.next_run_at(now)
    assert first == second
    base = first.replace(minute=0, second=0, microsecond=0)
    assert 0 <= (first - base).total_seconds() < 600


# ---------------------------------------------------------------------------
# run_once


@pytest.mark.asyncio
async def test_run_once_fetches_both_plans_and_writes():
    cache = FakeResultCache()
    rows: dict[str, tuple[dict[str, object], ...]] = {
        "GongziClQuery": ({"inputdate": "2026-07-01 10:00:00"},),
        "ScjdQuery": ({"dh": "20260701001"},),
    }
    calls: list[str] = []

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        calls.append(operation_id)
        plan_rows = rows[operation_id]
        return ResourceFetchResult(
            rows=plan_rows, total=len(plan_rows), pages_fetched=1, complete=True, footer=None
        )

    worker = make_worker(
        lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
        fetch=fetch,
        cache=cache,
    )
    stored = await worker.run_once()
    assert calls == ["GongziClQuery", "ScjdQuery"]
    assert stored == {"GongziClQuery": 1, "ScjdQuery": 1}
    assert [w[0] for w in cache.writes] == ["GongziClQuery", "ScjdQuery"]
    assert all(w[2] == "precompute" for w in cache.writes)
    # 预取参数与配方同参（§4.3）：两族各一次、固定 extra_params。
    assert len(PREFETCH_PLANS) == 2


@pytest.mark.asyncio
async def test_run_once_skips_incomplete_rows():
    cache = FakeResultCache()

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        return ResourceFetchResult(
            rows=({"a": "1"},),
            total=5,
            pages_fetched=1,
            complete=False,
            reason="total_drift",
        )

    worker = make_worker(
        lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
        fetch=fetch,
        cache=cache,
    )
    stored = await worker.run_once()
    assert stored == {"GongziClQuery": 0, "ScjdQuery": 0}
    assert cache.writes == []


@pytest.mark.asyncio
async def test_run_once_isolates_plan_failure_and_alerts():
    cache = FakeResultCache()
    alerts = AlertRecorder()

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        if operation_id == "GongziClQuery":
            raise RuntimeError("mes down")
        return ResourceFetchResult(
            rows=({"dh": "20260701001"},), total=1, pages_fetched=1, complete=True, footer=None
        )

    worker = make_worker(
        lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
        fetch=fetch,
        cache=cache,
        alerts=alerts,
    )
    stored = await worker.run_once()
    # 单族失败不影响另一族；失败告警但绝不抛出。
    assert stored == {"GongziClQuery": 0, "ScjdQuery": 1}
    assert cache.writes == [("ScjdQuery", ("2025-10-16", "2026-10-15"), "precompute")]
    assert [kind for kind, _ in alerts.calls] == ["cache_prefetch_fetch_failed"]


@pytest.mark.asyncio
async def test_run_once_alerts_when_rows_cannot_be_written():
    """取到行却写不进去 = 缓存存储不可达（端口/网络配错）→ 告警、行数记 0。

    这是最贵的一类静默失败：预取日志照打「done」，白天却依然每次实时取数。
    worker 必须把「有行但未落盘」显式暴露出来（2026-10-11 端口配错事故）。
    """
    alerts = AlertRecorder()

    class UnwritableCache(FakeResultCache):
        async def write(
            self,
            tenant_id: Any,
            operation_id: str,
            rows: tuple[Mapping[str, Any], ...],
            footer: Mapping[str, str] | None,
            extra_params: Mapping[str, Any],
            window: tuple[str, str],
            *,
            source: str,
        ) -> bool:
            self.writes.append((operation_id, (window[0], window[1]), source))
            return False  # 存储拒绝写入，但取数本身是成功的

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        return ResourceFetchResult(
            rows=({"dh": "20260701001"},), total=1, pages_fetched=1, complete=True, footer=None
        )

    worker = make_worker(
        lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
        fetch=fetch,
        cache=UnwritableCache(),
        alerts=alerts,
    )
    stored = await worker.run_once()
    # 有行却未落盘 → 全部计 0，且两族各告警一次。
    assert stored == {"GongziClQuery": 0, "ScjdQuery": 0}
    assert [kind for kind, _ in alerts.calls] == [
        "cache_prefetch_write_failed",
        "cache_prefetch_write_failed",
    ]


@pytest.mark.asyncio
async def test_run_once_resolves_tenant_lazily():
    cache = FakeResultCache()

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        return ResourceFetchResult(rows=(), total=0, pages_fetched=1, complete=True, footer=None)

    worker = V2PrefetchWorker(
        fetch,
        cast("V2ResultCache", cache),
        tenant_id=None,
        tenant_resolver=lambda: "lazy-tenant",
        clock=lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
    )
    await worker.run_once()
    assert len(cache.writes) == 2


# ---------------------------------------------------------------------------
# 启动预热（run_warmup_then_forever）


@pytest.mark.asyncio
async def test_warmup_runs_once_then_enters_forever(monkeypatch: pytest.MonkeyPatch):
    """启动预热 = 先整刷一轮（老板视角落盘），成功后才进入周期循环。"""
    cache = FakeResultCache()

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        return ResourceFetchResult(
            rows=({"dh": "20260701001"},), total=1, pages_fetched=1, complete=True, footer=None
        )

    worker = make_worker(
        lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
        fetch=fetch,
        cache=cache,
    )
    order: list[str] = []

    async def fake_forever() -> None:
        order.append("forever")

    monkeypatch.setattr(worker, "run_forever", fake_forever)
    await worker.run_warmup_then_forever()
    assert len(cache.writes) == 2  # 预热已整刷两族
    assert order == ["forever"]  # 预热之后才进入 forever


@pytest.mark.asyncio
async def test_warmup_failure_does_not_block_forever(monkeypatch: pytest.MonkeyPatch):
    """预热整体失败（run_once 之外的异常）：告警但不阻断启动，仍进入周期循环。"""
    alerts = AlertRecorder()

    class ExplodingCache(FakeResultCache):
        def prefetch_window(self, today: str) -> tuple[str, str]:
            raise RuntimeError("redis exploded at boot")

    exploding = ExplodingCache()

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        raise AssertionError("fetch should not be reached")

    worker = make_worker(
        lambda: datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc),
        fetch=fetch,
        cache=exploding,
        alerts=alerts,
    )
    order: list[str] = []

    async def fake_forever() -> None:
        order.append("forever")

    monkeypatch.setattr(worker, "run_forever", fake_forever)
    await worker.run_warmup_then_forever()
    assert order == ["forever"]
    assert [kind for kind, _ in alerts.calls] == ["cache_prefetch_warmup_failed"]
