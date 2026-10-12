"""V2 预取任务（老板缓存的夜间整刷，方案 §4.3）。

进程内 lifespan 周期任务（复用 ``RollupWorker`` 的生命周期模式，不引入新调度器）：
- 部署时区 **02:00** 触发（窗口止于「昨天」，而昨天的补录可能发生在当天 20:00
  之后；02:00 时这些已落库），每租户按 ``hash(tenant)`` 抖动错开；
- **两族各 1 次调用 / 租户 / 晚**：工资 ``GongziClQuery`` 明细模式（= 工资族预取
  粒度），进度 ``ScjdQuery`` 制单 × 部门（M7 = 进度预取粒度），窗口统一
  ``[今天 − 365, 昨天]``；
- 凭证从环境变量 ``FACTORY_AGENT_MES_USER_CREDENTIAL_99`` 读取（【决策】不另建
  服务账号机制），走真实 token 换取得到 99 的 ``DataScope``（``mes_filtered``）；
- 客户 MES 夜间无限流：按全量窗口直接拉，不分片、不退避；
- 行集经回写门禁 + ``dh_date`` 派生校验后整批入缓存（``source="precompute"``）。

未配置 Redis、未配置凭据或 MES 时 worker 不构建，部署完全不受影响（§4.7）。
"""

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.contracts import ResourceFetchResult

_LOGGER = get_logger("factory_agent.application.prefetch")

#: 预取计划（方案 §4.3 / 附录 C）：operation → 固定请求参数。
#: 与配方同参是硬约束：键归一化后必须与交互路径完全一致才能命中。
PREFETCH_PLANS: tuple[tuple[str, dict[str, Any]], ...] = (
    # 工资族预取粒度 = 明细模式（不传 groupbyFields），FR-002/003/012 同底料。
    ("GongziClQuery", {"Flag": "0", "Type": "0,1,2"}),
    # 进度预取粒度 = 制单 × 部门（M7），FR-005/009 列表层与 FR-013 KPI 的公共底料。
    (
        "ScjdQuery",
        {
            "groupbyFields": [
                "dh",
                "jhdh",
                "chuanghao",
                "huohao",
                "bbreed",
                "dept",
                "deptname",
            ]
        },
    ),
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

    def next_run_at(self, now: datetime | None = None) -> datetime:
        """下一个预取时点：今天/明天 02:00（部署时区）+ 租户抖动。"""
        current = (now or self._now()).astimezone(self._timezone)
        jitter = timedelta(seconds=self._jitter()) if self._jitter_seconds else timedelta(0)
        candidate = current.replace(
            hour=self._prefetch_hour, minute=0, second=0, microsecond=0
        ) + jitter
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
            _LOGGER.exception("v2_result.prefetch.warmup_failed")
            await self._alert(
                "cache_prefetch_warmup_failed", {"tenant": str(self._tenant())}
            )
        await self.run_forever()

    async def run_forever(self) -> None:
        while True:
            delay = (self.next_run_at() - self._now()).total_seconds()
            try:
                await asyncio.sleep(max(delay, self._poll_seconds))
            except asyncio.CancelledError:
                raise
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - 预取失败不致命，下一晚整刷
                _LOGGER.exception("v2_result.prefetch.failed")
                await self._alert(
                    "cache_prefetch_failed", {"tenant": str(self._tenant())}
                )

    # ----------------------------------------------------------------- run

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        """执行一轮预取；返回 {operation: 落盘行数}（未落盘计 0）。"""
        today = (now or self._now()).astimezone(self._timezone).date().isoformat()
        window = self._cache.prefetch_window(today)
        stored: dict[str, int] = {}
        for operation_id, extra_params in PREFETCH_PLANS:
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
                    {"operation": operation_id, "tenant": str(self._tenant())},
                )
                stored[operation_id] = 0
                continue
            rows = fetched.rows
            footer = fetched.footer
            if not fetched.complete:
                # 不完整行集绝不入缓存（回写门禁的完整性前置条件）。
                _LOGGER.warning(
                    "v2_result.prefetch.incomplete",
                    extra={"operation": operation_id, "reason": fetched.reason},
                )
                stored[operation_id] = 0
                continue
            written = await self._cache.write(
                self._tenant(),
                operation_id,
                rows,
                footer,
                extra_params,
                window,
                source="precompute",
            )
            if rows and not written:
                # 取到了行却写不进去 = 缓存存储不可达（端口/网络配错）。这是最贵的
                # 一类静默失败：日志照打「预取完成」，白天却依然每次实时取数。
                _LOGGER.warning(
                    "v2_result.prefetch.write_failed",
                    extra={"operation": operation_id, "rows": len(rows)},
                )
                await self._alert(
                    "cache_prefetch_write_failed",
                    {"operation": operation_id, "tenant": str(self._tenant())},
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


__all__ = ["PREFETCH_PLANS", "V2PrefetchWorker"]
