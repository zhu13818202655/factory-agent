from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from factory_agent.domain.errors import UpstreamInvalidError
from factory_agent.execution.dag import (
    ExecutionBudget,
    RecipeDagExecutor,
    StepOutcome,
)


@dataclass(frozen=True)
class Step:
    step_id: str
    kind: str = "api"
    operation_id: str | None = None
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class FakeRequest:
    operation_id: str | None
    query: tuple[tuple[str, str], ...]


def _factory(step: Step, filters: object) -> FakeRequest:
    return FakeRequest(operation_id=step.operation_id, query=(("f", "1"),))


def _runner(
    rows_by_operation: dict[str, list[dict[str, Any]]], fail: set[str] | None = None
) -> Any:
    fail = fail or set()
    calls: list[str] = []

    async def run(step: Step, filters: object, factory: object, tracker: object) -> StepOutcome:
        op = step.operation_id or step.step_id
        calls.append(op)
        if op in fail:
            raise UpstreamInvalidError("injected failure")
        rows = tuple(rows_by_operation.get(op, [{"row": op}]))
        return StepOutcome(step_id=step.step_id, status="complete", rows=rows)

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.mark.asyncio
async def test_parallel_steps_run_concurrently_and_complete() -> None:
    started = asyncio.Event()
    both_started: list[str] = []

    async def run(step: Step, filters: object, factory: object, tracker: object) -> StepOutcome:
        both_started.append(step.step_id)
        if len(both_started) == 2:
            started.set()
        # Both steps must be in flight before either completes.
        await asyncio.wait_for(started.wait(), timeout=1)
        return StepOutcome(step_id=step.step_id, status="complete")

    executor = RecipeDagExecutor(run)
    steps = (
        Step(step_id="a", operation_id="op-a", parallel_group="g1"),
        Step(step_id="b", operation_id="op-b", parallel_group="g1"),
    )
    result = await executor.execute(steps, filters=None, request_factory=_factory)
    assert result.complete is True
    assert sorted(both_started) == ["a", "b"]


@pytest.mark.asyncio
async def test_identical_requests_are_deduplicated() -> None:
    runner = _runner({})
    executor = RecipeDagExecutor(runner)
    steps = (
        Step(step_id="a", operation_id="same-op"),
        Step(step_id="b", operation_id="same-op"),
    )
    result = await executor.execute(steps, filters=None, request_factory=_factory)
    assert result.complete is True
    assert runner.calls == ["same-op"]  # second hit served from cache
    outcomes = {outcome.step_id: outcome for outcome in result.outcomes}
    assert outcomes["b"].reason == "deduplicated"


@pytest.mark.asyncio
async def test_call_budget_exhaustion_cancels_structurally() -> None:
    runner = _runner({})
    executor = RecipeDagExecutor(runner, budget=ExecutionBudget(max_api_calls=1))
    steps = (
        Step(step_id="a", operation_id="op-a"),
        Step(step_id="b", operation_id="op-b"),
    )
    result = await executor.execute(steps, filters=None, request_factory=_factory)
    assert result.complete is False
    assert result.cancel_reason == "api_call_budget_exhausted"


@pytest.mark.asyncio
async def test_optional_step_failure_marks_incomplete_not_error() -> None:
    runner = _runner({}, fail={"op-opt"})
    executor = RecipeDagExecutor(runner)
    steps = (
        Step(step_id="opt", operation_id="op-opt", optional=True),
        Step(step_id="main", operation_id="op-main"),
    )
    result = await executor.execute(steps, filters=None, request_factory=_factory)
    outcomes = {outcome.step_id: outcome for outcome in result.outcomes}
    assert outcomes["opt"].status == "incomplete"
    assert outcomes["main"].status == "complete"
    assert result.complete is False
    assert result.cancel_reason == "partial_failure"


@pytest.mark.asyncio
async def test_required_step_failure_propagates_structured_cancel() -> None:
    runner = _runner({}, fail={"op-main"})
    executor = RecipeDagExecutor(runner)
    steps = (Step(step_id="main", operation_id="op-main"),)
    result = await executor.execute(steps, filters=None, request_factory=_factory)
    assert result.complete is False
    assert result.cancel_reason == "mes_error:upstream_invalid"


@pytest.mark.asyncio
async def test_dependency_order_is_respected() -> None:
    order: list[str] = []

    async def run(step: Step, filters: object, factory: object, tracker: object) -> StepOutcome:
        order.append(step.step_id)
        return StepOutcome(step_id=step.step_id, status="complete")

    executor = RecipeDagExecutor(run)
    steps = (
        Step(step_id="child", operation_id="op-child", depends_on=("parent",)),
        Step(step_id="parent", operation_id="op-parent"),
    )
    await executor.execute(steps, filters=None, request_factory=_factory)
    assert order.index("parent") < order.index("child")


@pytest.mark.asyncio
async def test_row_budget_charging_cancels_execution() -> None:
    many_rows = tuple({"row": i} for i in range(100))

    # Give the runner a charge_rows method by wrapping the real tracker.
    async def charging_run(
        step: Step, filters: object, factory: object, tracker: object
    ) -> StepOutcome:
        if hasattr(tracker, "charge_rows"):
            tracker.charge_rows(len(many_rows))  # type: ignore[attr-defined]
        return StepOutcome(step_id=step.step_id, status="complete", rows=many_rows)

    executor = RecipeDagExecutor(charging_run, budget=ExecutionBudget(max_rows=150))
    steps = (
        Step(step_id="a", operation_id="op-a"),
        Step(step_id="b", operation_id="op-b"),
    )
    result = await executor.execute(steps, filters=None, request_factory=_factory)
    assert result.complete is False
    assert result.cancel_reason == "row_budget_exhausted"
