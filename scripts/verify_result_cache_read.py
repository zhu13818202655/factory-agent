"""离验证：**读路径**能不能命中已落盘的老板缓存（不写、不取数）。

与 ``diagnose_cache_prefill.py`` 互补——那个查「为什么写不进去」，这个查
「写进去了，读得出来吗」。复用 ``bootstrap`` 的 ``_build_cache`` /
``_build_result_cache`` / ``_Owner99PrefetchFetcher``，因此算出的键与运行中的
应用**逐字节一致**，不会出现「脚本命中、应用不命中」的假象。

它回答三个问题：

  1 用配方（recipe）的真实参数问一个 9 月窗口，**会不会命中**预取键？
  2 命中后按 ``scope_filtered_rows`` 过滤——**老板（99）看到多少行**？
  3 同一个命中行集，**组长（01，按 dept）看到多少行**？

关键设计（``result_cache.py`` 模块 docstring §4.2）：键里的 ``scope_sig``
**恒为 owner-99**，提问者的范围（组长的小组 / 员工的工号）**不进键**，
是读取时的过滤参数。所以组长与老板命中的是**同一个键**，差别只在读后过滤。

用法::

    uv run --no-sync python scripts/verify_result_cache_read.py
    uv run --no-sync python scripts/verify_result_cache_read.py --window 2026-09-01 2026-09-30
    uv run --no-sync python scripts/verify_result_cache_read.py --op GongziClQuery

退出码：0 = 至少一族命中；1 = 一族都没命中（并打印探测过的每一个键）。
"""

import argparse
import asyncio
import os
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_agent import bootstrap as boot
from factory_agent.data_api.result_cache import (
    _PROGRESS_M7_GROUPBY,  # noqa: PLC2701 - 与预取同参，刻意的
    PAYROLL_OPERATION,
    PROGRESS_OPERATION,
    RESULT_CACHE_DOMAIN,
    V2_RESULT_SCOPE_FINGERPRINT,
    scope_filtered_rows,
    time_anchor_column,
    time_sliced_rows,
)
from factory_agent.data_api.token_gateway import TokenCredentialExchange
from factory_agent.domain import NarrowedFilters

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

#: 与 ``prefetch.PREFETCH_PLANS`` 逐字对齐——键要一致，参数就必须一致。
PLANS: tuple[tuple[str, dict[str, Any]], ...] = (
    (PAYROLL_OPERATION, {"Flag": "0", "Type": "0,1,2"}),
    (PROGRESS_OPERATION, {"groupbyFields": sorted(_PROGRESS_M7_GROUPBY)}),
)


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


def candidate_keys(
    cache: Any,
    result_cache: Any,
    tenant: Any,
    op: str,
    extra_params: Mapping[str, Any],
    window: tuple[str, str],
) -> list[tuple[tuple[str, str], str]]:
    """复刻 ``V2ResultCache.read`` 的候选窗口顺序，给出 (窗口, 键) 列表。"""
    today = result_cache._today()  # noqa: SLF001 - 只读诊断
    prefetch = result_cache.prefetch_window(today)  # noqa: SLF001
    settled = window[0] >= prefetch[0] and window[1] <= prefetch[1]
    windows = [prefetch, window] if settled else [window]
    out: list[tuple[tuple[str, str], str]] = []
    for candidate in windows:
        key_params = result_cache._key_params(op, extra_params, candidate)  # noqa: SLF001
        key = cache.build_key(RESULT_CACHE_DOMAIN, tenant, V2_RESULT_SCOPE_FINGERPRINT, key_params)
        out.append((candidate, key))
    return out


async def verify_one(
    cache: Any,
    result_cache: Any,
    tenant: Any,
    op: str,
    extra_params: Mapping[str, Any],
    window: tuple[str, str],
) -> bool:
    rule(f"读路径验证 · {op}")
    params = result_cache._key_params(op, extra_params, window)  # noqa: SLF001
    info(f"问题窗口     = {window[0]} .. {window[1]}")
    info(f"归一化参数   = {params}")

    today = result_cache._today()  # noqa: SLF001
    prefetch = result_cache.prefetch_window(today)  # noqa: SLF001
    settled = window[0] >= prefetch[0] and window[1] <= prefetch[1]
    info(f"是否已结算   = {settled}（True → 先查预取键，再回退问题窗口键）")
    info(f"预取窗口     = {prefetch[0]} .. {prefetch[1]}")

    for candidate, key in candidate_keys(cache, result_cache, tenant, op, extra_params, window):
        lookup = await cache.get(
            RESULT_CACHE_DOMAIN,
            tenant,
            V2_RESULT_SCOPE_FINGERPRINT,
            result_cache._key_params(op, extra_params, candidate),
        )  # noqa: SLF001
        mark = "有" if lookup.value is not None else "无"
        info(f"  窗口 {candidate[0]}..{candidate[1]}  键 …{key[-40:]}  value={mark}")

    hit = await result_cache.read(tenant, op, params, window)
    if hit is None:
        bad(f"{op}：未命中（读会回退实时，这就是那 70~80 秒）")
        return False
    ok(f"{op}：命中！原始行集 {len(hit.rows)} 行，source={hit.source or '<空>'}")

    anchor = time_anchor_column(op, params)
    owner_rows = time_sliced_rows(hit.rows, anchor, window)
    ok(f"   老板(99)视图：范围过滤=不过滤 → 时间截取后 {len(owner_rows)} 行")

    depts = Counter(str(row.get("dept") or "") for row in owner_rows)
    depts.pop("", None)
    sample = [dept for dept, _ in depts.most_common(3)]
    if not sample:
        info("   行集里没有 dept 列（该操作无部门维度），跳过组长视图模拟")
        return True
    dept_counts = [depts[d] for d in sample]
    info(f"   行集里 dept 取值 {len(depts)} 个，最多的三个：{sample}（含 {dept_counts} 行）")

    for dept in sample:
        filters = NarrowedFilters(
            tenant_id=tenant,
            employee_ids=None,
            dept_ids=frozenset({dept}),
        )
        scoped = scope_filtered_rows(hit.rows, op, filters)
        sliced = time_sliced_rows(scoped, anchor, window)
        ok(f"   组长视图(dept={dept})：范围过滤后 {len(scoped)} 行 → 时间截取后 {len(sliced)} 行")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        nargs=2,
        default=["2026-09-01", "2026-09-30"],
        metavar=("DATES", "DATEE"),
        help="问题窗口，默认 2026 年 9 月",
    )
    parser.add_argument("--op", default=None, help="只验证某一族（GongziClQuery / ScjdQuery）")
    args = parser.parse_args()
    window = (args.window[0], args.window[1])

    rule("1/4 载入配置")
    load_env(ENV_FILE)
    settings = boot.FactoryAgentSettings()
    info(f"result_cache_mode = {settings.result_cache_mode}")

    rule("2/4 构建缓存（与运行中的应用同键）")
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

    rule("3/4 换取 99 凭据（只为拿 tenant_id）")
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
    await fetcher._bind()  # noqa: SLF001
    tenant = fetcher.tenant_id()
    ok(f"tenant_id = {tenant}")

    rule("4/4 逐族验证读路径")
    plans = tuple((op, params) for op, params in PLANS if args.op is None or op == args.op)
    hits = 0
    for op, params in plans:
        if await verify_one(cache, result_cache, tenant, op, params, window):
            hits += 1

    rule("结论")
    if hits:
        ok(f"{hits}/{len(plans)} 族命中——读通路是好的")
        print(
            "   注意：若 .env 是 shadow，命中后仍会实时取一次做比对（verifies=True），\n"
            "        所以命中也不会变快；只有 readwrite 才真正省掉这次实时取数。",
            flush=True,
        )
        return 0
    bad(f"{hits}/{len(plans)} 族命中——读通路有问题，或键根本没写进去")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
