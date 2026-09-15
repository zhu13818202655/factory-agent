"""Offline benchmark for the fan-out batch size (``MES_FANOUT_CONCURRENCY``).

A fan-out step issues one MES request per distinct bound value, so its wall
clock is dominated by upstream round-trip latency. This script measures how
much of that time the batching actually recovers, and proves the outcome does
not move: every concurrency level must report the identical rows, request count
and incompleteness state.

The upstream here is a simulated one with a configurable per-request latency.
That is deliberate: it isolates our own serialization from whatever the
customer MES does under load, which is the part we can control and the part
this knob changes. The remaining production gate — "the customer MES shows no
new 429/timeout at concurrency N" — needs a run against the real interface and
is not testable from this script.

Usage::

    .venv/bin/python scripts/bench_fanout.py
    .venv/bin/python scripts/bench_fanout.py --latency-ms 400 --combos 120
    .venv/bin/python scripts/bench_fanout.py --json

Run it from the repository root.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parents[1] / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_agent.application.filters import NarrowedFilters  # noqa: E402
from factory_agent.data_api.catalog import load_catalog  # noqa: E402
from factory_agent.data_api.schemas import ROW_MODEL_BY_RESOURCE  # noqa: E402
from factory_agent.domain import CapabilityId, TenantId, TimeRange  # noqa: E402
from factory_agent.execution.executor import ExecutionRequest  # noqa: E402
from factory_agent.execution.kernel import (  # noqa: E402
    KernelCapabilityRunner,
    KernelSettings,
)
from factory_agent.execution.recipes import load_recipes  # noqa: E402
from factory_agent.execution.result_table import default_metric_registry  # noqa: E402
from factory_agent.ports.contracts import ResourceFetchResult  # noqa: E402
from factory_agent.ports.session import CapabilityRunRequest  # noqa: E402

#: The capability whose recipe fans out one request per material.
CAPABILITY = "fr005_order_progress"


class SimulatedMes:
    """Upstream stand-in: fixed latency per fan-out request, rows in order."""

    def __init__(self, *, latency_seconds: float, materials: tuple[str, ...]) -> None:
        self.latency_seconds = latency_seconds
        self.materials = materials
        self.in_flight = 0
        self.max_in_flight = 0
        self.fanout_calls = 0

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> ResourceFetchResult:
        params = dict(extra_params or {})
        if request.operation_id == "WorktypeProgressQuery":
            self.fanout_calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(self.latency_seconds)
                material = params.get("userid", "")
                rows: tuple[dict[str, Any], ...] = (
                    {
                        "userid": material,
                        "worktype": "WT01",
                        "name": "平车",
                        "uid": f"0{material}",
                        "wsort": 1,
                    },
                )
                return ResourceFetchResult(
                    rows=rows, total=1, pages_fetched=1, complete=True, footer=None
                )
            finally:
                self.in_flight -= 1
        rows = self._rows_for(request.operation_id)
        return ResourceFetchResult(
            rows=rows, total=len(rows), pages_fetched=1, complete=True, footer=None
        )

    def _rows_for(self, operation_id: str) -> tuple[dict[str, Any], ...]:
        if operation_id == "PlanGridPageList":
            return tuple(
                {
                    "dh": f"PLAN-{index}",
                    "jhdh": f"JH-{index}",
                    "khddh": f"KHDD-{index}",
                    "huohao": "HH001",
                    "huohaoname": "模拟款A",
                    "khname": "客户甲",
                    "zsl": "100",
                    "ddsl": "100",
                    "zhdate": "2026-07-01",
                    "finish_date": "2026-07-31",
                    "dept": "dept-a1",
                }
                for index in (1, 2)
            )
        if operation_id == "SclzdGridPageList":
            return tuple(
                {
                    "id": material,
                    "dh": f"ZD-{material}",
                    "dddh": f"JH-{index % 2 + 1}",
                    "huohao": "HH001",
                    "sssl": "13",
                }
                for index, material in enumerate(self.materials)
            )
        if operation_id == "WskQuery":
            return ({"id": "1", "huohao": "HH001", "worktype": "WT01", "sl": "93"},)
        return ()


def resource_columns() -> dict[str, tuple[str, ...]]:
    catalog = load_catalog()
    columns: dict[str, tuple[str, ...]] = {}
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        model = ROW_MODEL_BY_RESOURCE.get(operation.resource) if operation.resource else None
        columns[operation_id] = tuple(model.model_fields) if model else ()
    return columns


async def one_run(*, concurrency: int, combos: int, latency_seconds: float) -> dict[str, Any]:
    materials = tuple(f"{1000 + index}" for index in range(1, combos + 1))
    upstream = SimulatedMes(latency_seconds=latency_seconds, materials=materials)
    runner = KernelCapabilityRunner(
        upstream,
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
        settings=KernelSettings(max_api_calls=500 + combos, fanout_concurrency=concurrency),
        clock=lambda: datetime(2026, 8, 21, 8, tzinfo=UTC),
        resource_columns=resource_columns(),
    )
    request = CapabilityRunRequest(
        capability_id=CapabilityId(CAPABILITY),
        filters=NarrowedFilters(tenant_id=TenantId("BENCH"), employee_ids=None, dept_ids=None),
        time_range=TimeRange(
            start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 31, tzinfo=UTC)
        ),
    )
    started = time.perf_counter()
    result = await runner.run(request)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "concurrency": concurrency,
        "wall_ms": round(elapsed_ms, 1),
        "fanout_calls": upstream.fanout_calls,
        "max_in_flight": upstream.max_in_flight,
        "api_call_count": result.api_call_count,
        "rows": len(result.rows),
        "incomplete": result.incomplete,
        "reason": result.incomplete_reason,
        "fingerprint": _fingerprint(result),
    }


def _fingerprint(result: Any) -> str:
    """Order-insensitive digest of the rendered rows, for cross-level equality."""
    return json.dumps(
        sorted(str(tuple(row)) for row in result.rows), ensure_ascii=False, sort_keys=True
    )


def _p(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combos", type=int, default=120, help="fan-out combinations")
    parser.add_argument("--latency-ms", type=float, default=300.0, help="per-request latency")
    parser.add_argument("--repeat", type=int, default=3, help="timed repeats per level")
    parser.add_argument("--levels", default="1,2,4,8", help="concurrency levels to compare")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    levels = [int(level) for level in args.levels.split(",") if level.strip()]
    latency_seconds = args.latency_ms / 1000.0
    samples: dict[int, list[float]] = {level: [] for level in levels}
    results: dict[int, dict[str, Any]] = {}

    for level in levels:
        for _ in range(max(1, args.repeat)):
            outcome = await one_run(
                concurrency=level, combos=args.combos, latency_seconds=latency_seconds
            )
            samples[level].append(float(outcome["wall_ms"]))
            results[level] = outcome

    baseline = results[levels[0]]
    mismatched = [
        level
        for level in levels
        if results[level]["fingerprint"] != baseline["fingerprint"]
        or results[level]["fanout_calls"] != baseline["fanout_calls"]
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "combos": args.combos,
                    "latency_ms": args.latency_ms,
                    "repeat": args.repeat,
                    "levels": [
                        {
                            **results[level],
                            "p50_ms": round(statistics.median(samples[level]), 1),
                            "best_ms": round(min(samples[level]), 1),
                        }
                        for level in levels
                    ],
                    "outcome_mismatch_levels": mismatched,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not mismatched else 1

    serial = statistics.median(samples[levels[0]]) or 1.0
    print(
        f"fan-out combos={args.combos} upstream latency={args.latency_ms:.0f}ms "
        f"repeats={args.repeat}"
    )
    print()
    print("| 并发 | p50 耗时(ms) | 最快(ms) | 相对串行 | fan-out 请求数 | 峰值并发 | 结果一致 |")
    print("|---|---|---|---|---|---|---|")
    for level in levels:
        p50 = statistics.median(samples[level])
        row = results[level]
        print(
            f"| {level} | {p50:.1f} | {min(samples[level]):.1f} | {serial / p50:.2f}x | "
            f"{row['fanout_calls']} | {row['max_in_flight']} | "
            f"{'一致' if level not in mismatched else '**不一致**'} |"
        )
    print()
    print(f"串行墙钟上界 ≈ combos × latency = {args.combos * args.latency_ms:.0f}ms")
    print(f"incomplete={baseline['incomplete']} reason={baseline['reason']}")
    if mismatched:
        print(f"ERROR: 并发 {mismatched} 的结果与串行不一致")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
