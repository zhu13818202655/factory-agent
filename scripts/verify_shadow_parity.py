"""影子（shadow）一致性现场验证：把**一条真实提问**跑完整条 shadow 通路并出对账。

与 ``verify_result_cache_read.py``（只读、不取数）互补——这个会真实调用 MES 做影子
复核，因此能回答那个最关键的问题：

    缓存切片 serve 出去的东西，和实时重取的结果，**到底一不一样**？

它一次打印三条口径，便于逐字对照：

  1 **缓存切片**：``read`` → 范围过滤 → 时间截取（0 次 MES）；
  2 **实时**    ：绕开缓存直接走 ``ScopedExecutor``（1 次 MES，约 70~80 秒）；
  3 **shadow**  ：走 ``ResultCacheExecutor``（mode=shadow → 命中后仍实时复核一次），
                 捕获 ``cache_shadow_mismatch`` 告警，并报告最终返回的是哪一份。

1 与 2 一致、且 3 不告警，才说明缓存可以放心切到 ``readwrite``。

注意：mode=shadow 且判定**不一致**时，执行器会驱逐命中的那个键。也就是说这个脚本
在「不一致」的情况下会把预取键删掉——这正是它的意图（坏条目不该留），但意味着
跑完要重新预取一次。

用法::

    uv run --no-sync python scripts/verify_shadow_parity.py
    uv run --no-sync python scripts/verify_shadow_parity.py --dept 001 --month 2026-09
    uv run --no-sync python scripts/verify_shadow_parity.py --no-realtime   # 省掉第 2 步

退出码：0 = 三条口径一致（或第 2 步被跳过）；1 = 不一致或未命中。
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_agent import bootstrap as boot
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.result_cache import (
    PAYROLL_OPERATION,
    ResultCacheExecutor,
    local_view_rows,
)
from factory_agent.data_api.token_gateway import TokenCredentialExchange
from factory_agent.domain import NarrowedFilters, Role, local_calendar_dates
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
UTC = ZoneInfo("UTC")

#: 与配方 fr008_payroll_ranking / 预取计划逐字对齐的取数参数。
PAYROLL_PARAMS: dict[str, Any] = {"Flag": "0", "Type": "0,1,2"}

#: 金额列（配方 fr008 里 ``SUM(je)`` 即工资额）。
AMOUNT_COLUMN = "je"


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}", flush=True)


def ok(message: str) -> None:
    print(f"   [ OK ] {message}", flush=True)


def bad(message: str) -> None:
    print(f"   [FAIL] {message}", flush=True)


def info(message: str) -> None:
    print(f"   [ .. ] {message}", flush=True)


def load_env(path: Path) -> None:
    """兜底把 .env 灌进 os.environ；已存在的真实环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def month_span(month: str, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """``"2026-09"`` → 厂区时区下的半开区间 ``[09-01T00:00, 10-01T00:00)``。"""
    year_text, _, month_text = month.partition("-")
    start = datetime(int(year_text), int(month_text), 1, tzinfo=zone)
    end = (
        datetime(start.year + 1, 1, 1, tzinfo=zone)
        if start.month == 12
        else datetime(start.year, start.month + 1, 1, tzinfo=zone)
    )
    return start, end


def summarize(rows: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    total = Decimal("0")
    for row in rows:
        value = row.get(AMOUNT_COLUMN)
        if value is None or value == "":
            continue
        try:
            total += Decimal(str(value))
        except InvalidOperation:
            continue
    return {
        "rows": len(rows),
        "people": len({str(row.get("uid") or "") for row in rows}),
        "amount": f"{total:.2f}",
    }


def report(label: str, stats: Mapping[str, Any]) -> None:
    info(
        f"{label:<12} 行={stats['rows']:<6} 人={stats['people']:<6} "
        f"Σ{AMOUNT_COLUMN}={stats['amount']}"
    )


def same(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return dict(left) == dict(right)


async def main() -> int:  # noqa: PLR0915 - 线性验证脚本，拆开反而更难逐条对照
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dept", default="001", help="部门（组长视角）；空串 = 老板全厂视角")
    parser.add_argument("--month", default="2026-09", help="月份，形如 2026-09")
    parser.add_argument("--no-realtime", action="store_true", help="跳过独立的实时口径")
    parser.add_argument(
        "--mode",
        default=None,
        help="覆盖 FACTORY_AGENT_RESULT_CACHE_MODE（off/read/readwrite/shadow）；"
        "观察影子比对必须用 shadow，否则 verifies=False、比对路径根本不被执行",
    )
    args = parser.parse_args()
    if args.mode:
        os.environ["FACTORY_AGENT_RESULT_CACHE_MODE"] = args.mode

    rule("1/5 载入配置")
    load_env(ENV_FILE)
    settings = boot.FactoryAgentSettings()
    zone = ZoneInfo(settings.factory_timezone)
    start, end = month_span(args.month, zone)
    info(f"factory_timezone  = {settings.factory_timezone}")
    info(f"result_cache_mode = {settings.result_cache_mode}")
    info(f"提问窗口（本地）  = {start.isoformat()} .. {end.isoformat()}（半开）")
    info(
        f"提问窗口（UTC）   = {start.astimezone(UTC).isoformat()} .. "
        f"{end.astimezone(UTC).isoformat()}"
    )

    rule("2/5 构建缓存（与运行中的应用同键）")
    cache = boot._build_cache(settings)  # noqa: SLF001 - 刻意的
    if cache is None:
        bad("Redis 未配置")
        return 1
    info(f"ping = {await cache.ping()}")
    result_cache = boot._build_result_cache(settings, cache, boot.SystemClock())  # noqa: SLF001
    if result_cache is None:
        bad("结果集缓存整体关闭（mode=off 或无 Redis）")
        return 1
    mode = result_cache.mode
    info(f"mode={mode.value}  reads={mode.reads}  writes={mode.writes}  verifies={mode.verifies}")

    rule("3/5 换取 99 凭据并构建真实取数链路")
    exchange = TokenCredentialExchange(
        str(settings.canonical_mes_base_url),
        refresh_threshold_seconds=settings.mes_token_refresh_threshold_seconds,
    )
    fetcher = boot._Owner99PrefetchFetcher(  # noqa: SLF001 - 与生产同一条链路
        settings,
        exchange,
        settings.mes_user_credential_99.get_secret_value().strip(),
        mes_recorder=boot.ContextVarMesCallRecorder(),
    )
    adapter = await fetcher._bind()  # noqa: SLF001
    tenant = fetcher.tenant_id()
    if tenant is None:
        bad("99 凭据换取后没有 tenant_id")
        return 1
    # ``ScopedExecutor`` 的请求类型比 ``FetchThrough`` 协议更具体（ExecutionRequest
    # vs CacheableRequest），生产侧同样用 ``Any`` 抹平（bootstrap 里就是 ``executor: Any``）。
    inner_executor: Any = ScopedExecutor(adapter=adapter, catalog=load_catalog())
    ok(f"tenant_id = {tenant}")

    filters = NarrowedFilters(
        tenant_id=tenant,
        employee_ids=None,
        dept_ids=frozenset({args.dept}) if args.dept else None,
    )
    request = ExecutionRequest(
        operation_id=PAYROLL_OPERATION,
        time_range=(start.astimezone(UTC), end.astimezone(UTC)),
        role=Role.OWNER,
    )
    info(f"范围过滤 = dept_ids={sorted(filters.dept_ids) if filters.dept_ids else 'None（全厂）'}")

    # ---- 口径 1：缓存切片（0 次 MES） --------------------------------------
    rule("4/5 口径 1 · 缓存切片（read → 范围过滤 → 时间截取，不取数）")
    window = local_calendar_dates(request.time_range[0], request.time_range[1], zone)
    key_params = result_cache._key_params(PAYROLL_OPERATION, PAYROLL_PARAMS, window)  # noqa: SLF001
    info(f"切片窗口   = {window[0]} .. {window[1]}   （修复后必须与实时同界）")
    info(f"问题窗口键参数 = {key_params}")
    hit = await result_cache.read(tenant, PAYROLL_OPERATION, PAYROLL_PARAMS, window)
    if hit is None:
        bad("未命中——先跑 scripts/diagnose_cache_prefill.py 把缓存写进去")
        return 1
    ok(f"命中！命中键窗口 = {hit.window[0]} .. {hit.window[1]}，原始行集 {len(hit.rows)} 行")
    cached_rows = local_view_rows(hit.rows, PAYROLL_OPERATION, filters, PAYROLL_PARAMS, window)
    cached_stats = summarize(cached_rows)
    report("缓存切片", cached_stats)

    # ---- 口径 2：实时（1 次 MES） ------------------------------------------
    # 注意：MES **不接受 dept 下推**（``hongzhao._resource_params`` 只为 GongziClQuery
    # 下推单个 ``uid``），部门维度一律由调用方本地过滤——缓存切片走
    # ``scope_filtered_rows``，配方走 compute 里的 ``WHERE dept IN (...)``。
    # 所以实时行集必须先做与缓存切片**同样的**本地过滤，否则比的是全厂 vs 一个部门。
    realtime_stats: dict[str, Any] | None = None
    if args.no_realtime:
        info("跳过（--no-realtime）")
    else:
        rule("5/5 口径 2 · 实时（绕开缓存直接取数，这一步最慢）")
        realtime = await inner_executor.execute_full_step(
            filters, request, extra_params=PAYROLL_PARAMS
        )
        report("实时(原始)", summarize(realtime.rows))
        realtime_stats = summarize(
            local_view_rows(realtime.rows, PAYROLL_OPERATION, filters, PAYROLL_PARAMS, window)
        )
        report("实时(同口径)", realtime_stats)

    # ---- 口径 3：shadow 通路（命中 + 实时复核） -----------------------------
    rule("5/5 口径 3 · 通路（shadow = 命中后仍实时复核一次；readwrite = 纯读缓存，0 次 MES）")
    alerts: list[tuple[str, dict[str, Any]]] = []

    def record_alert(name: str, detail: dict[str, Any]) -> None:
        alerts.append((name, detail))
        bad(f"告警 {name}：{detail}")

    executor = ResultCacheExecutor(inner_executor, result_cache, alerts=record_alert)
    served = await executor.execute_full_step(filters, request, extra_params=PAYROLL_PARAMS)
    served_stats = summarize(served.rows)
    report("shadow 返回", served_stats)

    rule("结论")
    if realtime_stats is None:
        realtime_line = "未比较"
    else:
        realtime_line = "一致" if same(cached_stats, realtime_stats) else "不一致"
    ok(f"缓存切片 vs 实时    ：{realtime_line}")
    ok(f"shadow 返回 vs 缓存切片：{'一致' if same(served_stats, cached_stats) else '不一致'}")
    ok(f"shadow 告警数        = {len(alerts)}（0 = 判定一致）")
    if not alerts and (realtime_stats is None or same(cached_stats, realtime_stats)):
        ok("三条口径一致 → 缓存可以切 readwrite")
        return 0
    bad("存在不一致——缓存切片与实时不同界，或影子判定失败")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
