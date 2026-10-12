"""诊断授权老板的默认明细预取和回写。正常运行会访问配置的客户 MES 并写 Redis。

--dry-run 只取数和验证门禁；--reuse-fetch 只检查当前 Redis 快照，不再保存或复用
原始响应临时文件。输出仅含租户摘要、计数和状态。--help 不加载 .env。
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from cache_diagnostic_support import (
    ROOT,
    build_runtime,
    close_runtime,
    error_category,
    owner_fetchers,
    result_cache,
    tenant_label,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="已有环境变量优先")
    parser.add_argument("--dry-run", action="store_true", help="取默认明细并验证，不写 Redis")
    parser.add_argument("--reuse-fetch", action="store_true", help="只检查 Redis 已有快照")
    parser.add_argument("--only", choices=("GongziClQuery", "ScjdQuery"))
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from factory_agent.application.prefetch import PREFETCH_PLANS
    from factory_agent.data_api.progress_detail import derive_progress_rows
    from factory_agent.data_api.result_cache import PROGRESS_OPERATION, footer_reconciles

    container = build_runtime(args.env_file)
    successes = failures = 0
    try:
        cache = result_cache(container)
        owners = owner_fetchers(container)
        print(f"mode={cache.mode.value} producers={len(owners)}")
        if container.cache is None or not await container.cache.ping():
            print("Redis unavailable; no prefill writes attempted")
            return 1
        today = (
            container.clock.now().astimezone(ZoneInfo(container.settings.factory_timezone)).date()
        )
        window = cache.prefetch_window(today.isoformat())
        print(f"window={window[0]}..{window[1]} value_grain=default-detail")
        plans = (
            (op, params) for op, params in PREFETCH_PLANS if args.only is None or op == args.only
        )
        selected = tuple(plans)
        for index, owner in enumerate(owners):
            try:
                await owner.initialize()
                tenant = owner.tenant_id()
                if tenant is None:
                    raise RuntimeError("owner tenant is unavailable")
                label = tenant_label(tenant)
            except Exception as error:  # noqa: BLE001 - independent producer failures
                print(f"producer={index} initialized=False category={error_category(error)}")
                failures += 1
                continue
            for operation, params in selected:
                started = time.perf_counter()
                try:
                    if args.reuse_fetch:
                        hit = await cache.read(tenant, operation, params, window)
                        exists = hit is not None
                        print(f"tenant={label} operation={operation} cached={exists}")
                        successes += int(exists)
                        failures += int(not exists)
                        continue
                    fetched = await owner(operation, window, params)
                    gate = fetched.complete and footer_reconciles(
                        fetched.rows, fetched.footer, tolerance=cache.policy.float_tolerance
                    )
                    if gate and operation == PROGRESS_OPERATION:
                        derive_progress_rows(fetched.rows)
                    written = False
                    if gate and not args.dry_run:
                        if not cache.mode.writes:
                            raise RuntimeError("configured mode does not allow writes")
                        written = await cache.write(
                            tenant,
                            operation,
                            fetched.rows,
                            fetched.footer,
                            params,
                            window,
                            source="precompute",
                        )
                    passed = gate if args.dry_run else written
                    elapsed = (time.perf_counter() - started) * 1000
                    print(
                        f"tenant={label} operation={operation} rows={len(fetched.rows)} "
                        f"complete={fetched.complete} gate={gate} written={written} "
                        f"elapsed_ms={elapsed:.0f}"
                    )
                    successes += int(passed)
                    failures += int(not passed)
                except Exception as error:  # noqa: BLE001 - safe category only
                    print(f"tenant={label} operation={operation} category={error_category(error)}")
                    failures += 1
        print(f"passed={successes} failed={failures} dry_run={args.dry_run}")
        return 0 if successes and not failures else 1
    finally:
        await close_runtime(container)


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    try:
        return asyncio.run(run(args))
    except Exception as error:  # noqa: BLE001 - no raw configuration or response values
        print(f"diagnostic_failed category={error_category(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
