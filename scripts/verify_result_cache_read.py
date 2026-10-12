"""读取两族默认明细缓存并验证时间切片，不取业务明细、不写 Redis。

使用生产 owner provider 验证身份和租户；该授权过程可能访问 token / 组织资料。
默认窗口为工厂时区上个月。只输出租户摘要、计数和状态，--help 不加载 .env。
"""

import argparse
import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from cache_diagnostic_support import (
    ROOT,
    build_runtime,
    close_runtime,
    error_category,
    owner_fetchers,
    question_window,
    result_cache,
    tenant_label,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="已有环境变量优先")
    parser.add_argument("--window", nargs=2, metavar=("DATES", "DATEE"))
    parser.add_argument("--op", choices=("GongziClQuery", "ScjdQuery"))
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from factory_agent.application.prefetch import PREFETCH_PLANS
    from factory_agent.data_api.result_cache import time_anchor_column, time_sliced_rows

    container = build_runtime(args.env_file)
    hits = failures = 0
    try:
        cache = result_cache(container)
        owners = owner_fetchers(container)
        window = question_window(None, args.window, ZoneInfo(container.settings.factory_timezone))
        print(f"mode={cache.mode.value} window={window[0]}..{window[1]} producers={len(owners)}")
        plans = tuple(
            (op, params) for op, params in PREFETCH_PLANS if args.op is None or op == args.op
        )
        for index, owner in enumerate(owners):
            try:
                await owner.initialize()
                tenant = owner.tenant_id()
                if tenant is None:
                    raise RuntimeError("owner tenant is unavailable")
                label = tenant_label(tenant)
                for operation, params in plans:
                    hit = await cache.read(tenant, operation, params, window)
                    if hit is None:
                        print(f"tenant={label} operation={operation} hit=False")
                        failures += 1
                        continue
                    rows = time_sliced_rows(hit.rows, time_anchor_column(operation, params), window)
                    print(
                        f"tenant={label} operation={operation} hit=True source={hit.source} "
                        f"snapshot_rows={len(hit.rows)} sliced_rows={len(rows)} "
                        f"snapshot_window={hit.window[0]}..{hit.window[1]}"
                    )
                    hits += 1
            except Exception as error:  # noqa: BLE001 - safe identity diagnostics only
                print(f"producer={index} category={error_category(error)}")
                failures += 1
        print(f"hits={hits} failed={failures} business_detail_fetches=0")
        return 0 if hits and not failures else 1
    finally:
        await close_runtime(container)


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    try:
        return asyncio.run(run(args))
    except Exception as error:  # noqa: BLE001 - do not echo configuration values
        print(f"verification_failed category={error_category(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
