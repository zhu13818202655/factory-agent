"""Bounded DAG executor for L1 capability recipes.

Executes recipe steps with parallel groups running concurrently and groups
running serially. Deduplicates identical requests, enforces call/page/row/time
budgets, propagates structured cancellation, and marks optional-step failures
as explicit ``incomplete`` data instead of fabricating numbers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal

from factory_agent.domain.errors import InternalError, MesError, UpstreamInvalidError


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """Conservative first-release budgets, to be tuned against load measurements."""

    max_api_calls: int = 20
    max_pages: int = 100
    max_rows: int = 20000
    time_limit_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class StepOutcome:
    step_id: str
    status: Literal["complete", "incomplete", "failed", "skipped"]
    rows: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DagResult:
    outcomes: tuple[StepOutcome, ...]
    complete: bool
    cancel_reason: str | None = None

    def rows_for(self, step_id: str) -> tuple[dict[str, Any], ...]:
        for outcome in self.outcomes:
            if outcome.step_id == step_id:
                return outcome.rows
        return ()


class BudgetExceededError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BudgetTracker:
    def __init__(self, budget: ExecutionBudget) -> None:
        self._budget = budget
        self.api_calls = 0
        self.pages = 0
        self.rows = 0
        self.started_at = time.monotonic()

    def charge_call(self) -> None:
        self.api_calls += 1
        if self.api_calls > self._budget.max_api_calls:
            raise BudgetExceededError("api_call_budget_exhausted")

    def charge_pages(self, count: int) -> None:
        self.pages += count
        if self.pages > self._budget.max_pages:
            raise BudgetExceededError("page_budget_exhausted")

    def charge_rows(self, count: int) -> None:
        self.rows += count
        if self.rows > self._budget.max_rows:
            raise BudgetExceededError("row_budget_exhausted")

    def check_time(self) -> None:
        if time.monotonic() - self.started_at > self._budget.time_limit_seconds:
            raise BudgetExceededError("time_budget_exhausted")


StepKey = tuple[str, str]  # (operation_id, canonical params repr)


class RecipeDagExecutor:
    """Executes one validated capability recipe against a scoped executor."""

    def __init__(
        self,
        step_runner: Any,
        budget: ExecutionBudget | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._step_runner = step_runner
        self._budget = budget or ExecutionBudget()
        self._max_concurrency = max_concurrency

    async def execute(
        self,
        steps: tuple[Any, ...],
        filters: Any,
        request_factory: Any,
    ) -> DagResult:
        tracker = _BudgetTracker(self._budget)
        results: dict[str, StepOutcome] = {}
        cache: dict[StepKey, StepOutcome] = {}

        groups = _parallel_groups(steps)
        try:
            for group in groups:
                tracker.check_time()
                runnable = [
                    step for step in group if all(dep in results for dep in step.depends_on)
                ]
                executed = await asyncio.gather(
                    *(
                        self._run_step(step, filters, request_factory, tracker, results, cache)
                        for step in runnable
                    ),
                    return_exceptions=True,
                )
                for step, outcome in zip(runnable, executed, strict=True):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    results[step.step_id] = outcome
                for step in group:
                    if step.step_id not in results:
                        missing = [dep for dep in step.depends_on if dep not in results]
                        if missing:
                            results[step.step_id] = StepOutcome(
                                step_id=step.step_id,
                                status="skipped",
                                reason="dependency_failed",
                            )
        except BudgetExceededError as error:
            return DagResult(
                outcomes=tuple(results.values()),
                complete=False,
                cancel_reason=error.reason,
            )
        except MesError as error:
            return DagResult(
                outcomes=tuple(results.values()),
                complete=False,
                cancel_reason=f"mes_error:{error.code.value}",
            )

        complete = all(outcome.status in ("complete", "skipped") for outcome in results.values())
        return DagResult(
            outcomes=tuple(results.values()),
            complete=complete,
            cancel_reason=None if complete else "partial_failure",
        )

    async def _run_step(
        self,
        step: Any,
        filters: Any,
        request_factory: Any,
        tracker: _BudgetTracker,
        results: dict[str, StepOutcome],
        cache: dict[StepKey, StepOutcome],
    ) -> StepOutcome:
        if step.kind != "api":
            return StepOutcome(step_id=step.step_id, status="complete")

        key = _cache_key(step, filters, request_factory)
        cached = cache.get(key)
        if cached is not None:
            return StepOutcome(
                step_id=step.step_id,
                status=cached.status,
                rows=cached.rows,
                reason="deduplicated" if cached.status == "complete" else cached.reason,
            )

        tracker.charge_call()
        try:
            outcome = await self._step_runner(step, filters, request_factory, tracker)
        except UpstreamInvalidError:
            if step.optional:
                return StepOutcome(
                    step_id=step.step_id, status="incomplete", reason="upstream_invalid"
                )
            raise
        except MesError:
            if step.optional:
                return StepOutcome(
                    step_id=step.step_id, status="incomplete", reason="optional_failed"
                )
            raise
        cache[key] = outcome
        return StepOutcome(step_id=step.step_id, status=outcome.status, rows=outcome.rows)


def _cache_key(step: Any, filters: Any, request_factory: Any) -> StepKey:
    request = request_factory(step, filters)
    return (request.operation_id, repr(tuple(sorted(request.query))))


def _parallel_groups(steps: tuple[Any, ...]) -> list[list[Any]]:
    """Group steps into serial waves; within a wave, parallel_group members run concurrently.

    Dependencies always force a later wave. Steps without a parallel group run
    one per wave to keep deterministic ordering.
    """
    remaining = {step.step_id: step for step in steps}
    done: set[str] = set()
    waves: list[list[Any]] = []

    while remaining:
        ready = [step for step in remaining.values() if all(dep in done for dep in step.depends_on)]
        if not ready:
            raise InternalError("recipe dependency graph is cyclic")
        wave: list[Any] = []
        used_groups: set[str] = set()
        solo: list[Any] = []
        for step in ready:
            if step.parallel_group is not None:
                # All members of one parallel group run together in this wave.
                wave.append(step)
                used_groups.add(step.parallel_group)
            else:
                solo.append(step)
        if not wave and solo:
            wave.append(solo.pop(0))
        for step in wave:
            del remaining[step.step_id]
            done.add(step.step_id)
        waves.append(wave)

    return waves


__all__ = [
    "DagResult",
    "ExecutionBudget",
    "RecipeDagExecutor",
    "StepOutcome",
]
