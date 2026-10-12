"""Authorized tenant-local detail prefetch, coordinated across application replicas.

Each factory refreshes its two complete detail datasets at local 02:00. Redis
leases prevent overlapping producers; completed-day markers avoid repeated startup
fetches. Failed or partial runs remain retryable and never block business answers.
"""

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from factory_agent.application.cache import AuthAwareCache
from factory_agent.domain import TenantId
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.contracts import ResourceFetchResult

_LOGGER = get_logger("factory_agent.application.prefetch")

PREFETCH_PLANS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("GongziClQuery", {"Flag": "0", "Type": "0,1,2"}),
    ("ScjdQuery", {}),
)


class PrefetchFetch(Protocol):
    """一次预取取数：由 bootstrap 用 99 凭据的专用适配器实现。"""

    async def __call__(
        self,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult: ...


class PrefetchCache(Protocol):
    """结果集缓存的最小结构视图（避免 ``application → data_api`` 依赖）。

    ``V2ResultCache`` 在运行时满足本协议；worker 只用这两个方法。
    """

    def prefetch_window(self, today: str) -> tuple[str, str]: ...

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
        deadline: float | None = None,
    ) -> bool: ...


class V2PrefetchWorker:
    """每天 02:00（部署时区）+ 租户抖动，整刷两族预取结果集。"""

    def __init__(
        self,
        fetch: PrefetchFetch,
        cache: PrefetchCache,
        tenant_id: Any = None,
        *,
        tenant_resolver: Callable[[], Any] | None = None,
        prefetch_hour: int = 2,
        jitter_seconds: int = 0,
        poll_seconds: int = 300,
        timezone_name: str = "Asia/Shanghai",
        clock: Callable[[], datetime] | None = None,
        alerts: Any | None = None,
        prepare: Callable[[], Awaitable[None]] | None = None,
        coordination: AuthAwareCache | None = None,
        run_timeout_seconds: float = 600.0,
    ) -> None:
        self._fetch = fetch
        self._cache = cache
        self._static_tenant = tenant_id
        self._tenant_resolver = tenant_resolver
        self._prefetch_hour = prefetch_hour
        self._jitter_seconds = jitter_seconds
        self._poll_seconds = max(int(poll_seconds), 30)
        self._timezone = ZoneInfo(timezone_name)
        self._clock = clock
        self._alerts = alerts
        self._prepare = prepare
        self._coordination = coordination
        self._run_lock = asyncio.Lock()
        self._retry_needed = False
        if not math.isfinite(run_timeout_seconds) or run_timeout_seconds <= 0:
            raise ValueError("prefetch timeout must be positive")
        self._run_timeout_seconds = run_timeout_seconds

    # ------------------------------------------------------------- schedule

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        from datetime import timezone as _tz

        return datetime.now(_tz.utc)

    def _tenant(self) -> Any:
        if self._tenant_resolver is not None:
            return self._tenant_resolver()
        return self._static_tenant

    def _jitter(self) -> int:
        digest = hashlib.sha256(str(self._tenant()).encode()).hexdigest()
        return int(digest[:8], 16) % max(self._jitter_seconds, 1)

    def _tenant_label(self) -> str:
        return hashlib.sha256(str(self._tenant()).encode()).hexdigest()

    def next_run_at(self, now: datetime | None = None) -> datetime:
        """下一个预取时点：今天/明天 02:00（部署时区）+ 租户抖动。"""
        current = (now or self._now()).astimezone(self._timezone)
        jitter = timedelta(seconds=self._jitter()) if self._jitter_seconds else timedelta(0)
        candidate = (
            current.replace(hour=self._prefetch_hour, minute=0, second=0, microsecond=0) + jitter
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate

    async def run_warmup_then_forever(self) -> None:
        """启动即以老板（99）视角整刷一轮缓存，随后进入每日 02:00 周期。

        预热失败不阻断启动（与回写/读取同一条铁律：缓存是优化，不是闸门）——
        告警后仍进入周期循环，由下一时点补齐。
        """
        try:
            await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 预热失败不致命
            self._retry_needed = True
            _LOGGER.exception("v2_result.prefetch.warmup_failed")
            await self._alert("cache_prefetch_warmup_failed", {"tenant": self._tenant_label()})
        await self.run_forever()

    async def run_forever(self) -> None:
        while True:
            delay = (
                self._poll_seconds
                if self._retry_needed
                else (self.next_run_at() - self._now()).total_seconds()
            )
            try:
                await asyncio.sleep(max(delay, self._poll_seconds))
            except asyncio.CancelledError:
                raise
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - 预取失败不致命，下一晚整刷
                self._retry_needed = True
                _LOGGER.exception("v2_result.prefetch.failed")
                await self._alert("cache_prefetch_failed", {"tenant": self._tenant_label()})

    # ----------------------------------------------------------------- run

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        """Refresh once, reporting only rows confirmed written to the cache."""
        async with self._run_lock:
            deadline = asyncio.get_running_loop().time() + self._run_timeout_seconds
            job_now = now if now is not None else self._now()
            try:
                async with asyncio.timeout_at(deadline):
                    return await self._run_bounded(job_now, deadline)
            except TimeoutError:
                self._retry_needed = True
                _LOGGER.warning("v2_result.prefetch.timeout")
                await self._alert("cache_prefetch_timeout", {"tenant": self._tenant_label()})
                return {}

    async def _run_bounded(self, now: datetime, deadline: float) -> dict[str, int]:
        if self._prepare is not None:
            await self._prepare()
        _check_deadline(deadline)
        if self._coordination is None:
            result = await self._refresh(now, deadline)
            self._retry_needed = not all(self._write_results.values())
            return result
        tenant = self._tenant()
        if tenant is None:
            raise RuntimeError("prefetch tenant is not authorized")
        tenant_id = TenantId(str(tenant))
        local_now = now.astimezone(self._timezone)
        day = local_now.date().isoformat()
        params: dict[str, object] = {"job": "detail-prefetch", "day": day}
        marker_params = {
            **params,
            "phase": "warmup" if local_now.hour < self._prefetch_hour else "daily",
        }
        lease_seconds = math.ceil(self._run_timeout_seconds) + 30
        async with self._coordination.lease(tenant_id, params, lease_seconds) as acquired:
            if not acquired:
                self._retry_needed = True
                return {}
            lookup = await self._coordination.get(
                "v2_result", tenant_id, "prefetch-completed", marker_params
            )
            _check_deadline(deadline)
            if lookup.value == b"complete":
                self._retry_needed = False
                return {}
            result = await self._refresh(now, deadline)
            self._retry_needed = not all(self._write_results.values())
            if all(self._write_results.values()):
                _check_deadline(deadline)
                marker_written = await self._coordination.put(
                    "v2_result", tenant_id, "prefetch-completed", marker_params, b"complete"
                )
                _check_deadline(deadline)
                if not marker_written:
                    self._retry_needed = True
                    _LOGGER.warning("v2_result.prefetch.marker_write_failed")
                    await self._alert(
                        "cache_prefetch_marker_write_failed", {"tenant": self._tenant_label()}
                    )
            return result

    async def _refresh(
        self, now: datetime | None = None, deadline: float | None = None
    ) -> dict[str, int]:
        today = (now or self._now()).astimezone(self._timezone).date().isoformat()
        window = self._cache.prefetch_window(today)
        stored: dict[str, int] = {}
        self._write_results: dict[str, bool] = {}
        for operation_id, extra_params in PREFETCH_PLANS:
            _check_deadline(deadline)
            rows: tuple[Mapping[str, Any], ...] = ()
            footer = None
            try:
                fetched = await self._fetch(operation_id, window, extra_params)
            except Exception:  # noqa: BLE001 - 单族失败不影响另一族
                _LOGGER.exception(
                    "v2_result.prefetch.fetch_failed", extra={"operation": operation_id}
                )
                await self._alert(
                    "cache_prefetch_fetch_failed",
                    {"operation": operation_id, "tenant": self._tenant_label()},
                )
                stored[operation_id] = 0
                self._write_results[operation_id] = False
                continue
            _check_deadline(deadline)
            rows = fetched.rows
            footer = fetched.footer
            if not fetched.complete:
                # 不完整行集绝不入缓存（回写门禁的完整性前置条件）。
                _LOGGER.warning(
                    "v2_result.prefetch.incomplete",
                    extra={"operation": operation_id, "reason": fetched.reason},
                )
                stored[operation_id] = 0
                self._write_results[operation_id] = False
                continue
            written = await self._cache.write(
                self._tenant(),
                operation_id,
                rows,
                footer,
                extra_params,
                window,
                source="precompute",
                deadline=deadline,
            )
            _check_deadline(deadline)
            self._write_results[operation_id] = written
            if not written:
                # Complete empty snapshots must also be written; otherwise their
                # repeated queries still fetch MES and the daily job must retry.
                _LOGGER.warning(
                    "v2_result.prefetch.write_failed",
                    extra={"operation": operation_id, "rows": len(rows)},
                )
                await self._alert(
                    "cache_prefetch_write_failed",
                    {"operation": operation_id, "tenant": self._tenant_label()},
                )
            stored[operation_id] = len(rows) if written else 0
        _LOGGER.info("v2_result.prefetch.done", extra={"rows": stored})
        return stored

    async def _alert(self, kind: str, detail: dict[str, Any]) -> None:
        if self._alerts is None:
            return
        try:
            await self._alerts.alert(kind, detail)
        except Exception:  # noqa: BLE001 - 告警绝不影响预取
            _LOGGER.exception("v2_result.prefetch.alert_failed")


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError("prefetch deadline exceeded")


__all__ = ["PREFETCH_PLANS", "V2PrefetchGroup", "V2PrefetchWorker"]


class V2PrefetchGroup:
    """Run independent authorized factory workers and close their producer resources."""

    def __init__(
        self,
        workers: tuple[V2PrefetchWorker, ...],
        close: Callable[[], Awaitable[None]],
    ) -> None:
        self._workers = workers
        self._close = close

    async def run_warmup_then_forever(self) -> None:
        await asyncio.gather(*(worker.run_warmup_then_forever() for worker in self._workers))

    async def aclose(self) -> None:
        await self._close()
