"""在生产 recipe 内核比较最终结果、排名、合计和卡片的缓存/实时一致性。

默认以授权老板执行工资清单，--dept 只收窄该老板查询，不模拟组长身份。
shadow 会访问客户 MES；不一致由内核回退实时并驱逐坏键。默认另跑 off 基线；
--no-realtime 只省略独立基线，不省略 shadow 自身的实时验证。输出不含结果行、
金额、部门或凭据。--help 不加载 .env。本工具不替代各真实角色和下钻验收。
"""

import argparse
import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from cache_diagnostic_support import (
    ROOT,
    build_runtime,
    canonical_interval,
    close_runtime,
    equal_results,
    error_category,
    owner_fetchers,
    question_window,
    tenant_label,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="已有环境变量优先")
    parser.add_argument("--owner-index", type=int, default=0)
    parser.add_argument("--dept", default="", help="老板请求的部门收窄；空值查询获准范围")
    parser.add_argument("--month", help="YYYY-MM；默认工厂时区上个月")
    parser.add_argument(
        "--capability", default="fr008_payroll_ranking", help="不需要下钻槽位的 recipe id"
    )
    parser.add_argument("--no-realtime", action="store_true", help="省略独立 off 基线")
    parser.add_argument("--mode", choices=("off", "read", "readwrite", "shadow"), default="shadow")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from factory_agent.bootstrap import DependencyOverrides, build_container
    from factory_agent.domain import CapabilityId, DeptId, NarrowedFilters, Role, TimeRange
    from factory_agent.ports.contracts import TrustedCredential
    from factory_agent.ports.session import CapabilityRunRequest

    container = build_runtime(args.env_file, args.mode)
    baseline = None
    try:
        if args.mode != "shadow":
            print("not_verified: final recipe parity requires --mode shadow")
            return 2
        owners = owner_fetchers(container)
        if not 0 <= args.owner_index < len(owners):
            raise ValueError("owner producer index is invalid")
        context, scope = await owners[args.owner_index].authorization_context()
        exchange = container.credential_exchange
        runner = container.capability_runner
        if exchange is None or runner is None or container.cache is None:
            raise RuntimeError("production recipe runtime is unavailable")
        window = question_window(args.month, None, ZoneInfo(container.settings.factory_timezone))
        interval = TimeRange(
            *canonical_interval(window, ZoneInfo(container.settings.factory_timezone))
        )
        self_only = args.capability.startswith(("fr001_", "fr002_", "fr003_", "fr012_"))
        filters = NarrowedFilters(
            tenant_id=scope.tenant_id,
            employee_ids=frozenset({context.employee_id}) if self_only else None,
            dept_ids=None,
            requested_dept_ids=frozenset({DeptId(args.dept)}) if args.dept else None,
        )
        request = CapabilityRunRequest(
            capability_id=CapabilityId(args.capability),
            filters=filters,
            time_range=interval,
            role=Role.OWNER,
            caller_employee_id=context.employee_id,
            caller_dept_ids=scope.dept_ids,
        )
        credential = TrustedCredential(context.tenant_id, context.user_id)
        before_hits = container.cache.stats.hits["v2_result"]
        with exchange.bind_for(credential):
            checked = await runner.run(request)
        cache_read_hit = container.cache.stats.hits["v2_result"] > before_hits
        cache_accepted = checked.cache_source is not None and cache_read_hit
        print(
            f"tenant={tenant_label(scope.tenant_id)} capability={args.capability} "
            f"window={window[0]}..{window[1]} final_rows={len(checked.rows)} "
            f"incomplete={checked.incomplete} cache_read_hit={cache_read_hit} "
            f"cache_accepted={cache_accepted} api_calls={checked.api_call_count}"
        )
        baseline_equal = True
        if not args.no_realtime:
            baseline = build_container(
                container.settings.model_copy(update={"result_cache_mode": "off"}),
                DependencyOverrides(credential_exchange=exchange, clock=container.clock),
            )
            if baseline.capability_runner is None:
                raise RuntimeError("off baseline recipe runtime is unavailable")
            with exchange.bind_for(credential):
                live = await baseline.capability_runner.run(request)
            baseline_equal = equal_results(checked, live)
            print(
                f"off_baseline_equal={baseline_equal} final_rows={len(live.rows)} "
                f"api_calls={live.api_call_count}"
            )
        print(
            f"verified={cache_accepted and baseline_equal} "
            f"independent_baseline={not args.no_realtime}"
        )
        return 0 if cache_accepted and baseline_equal else 1
    finally:
        if baseline is not None:
            await close_runtime(baseline, close_exchange=False)
        await close_runtime(container)


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    try:
        return asyncio.run(run(args))
    except Exception as error:  # noqa: BLE001 - never print row-bearing exceptions
        print(f"verification_failed category={error_category(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
