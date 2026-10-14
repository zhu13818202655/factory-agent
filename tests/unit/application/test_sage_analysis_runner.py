import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal, cast

import pytest
from sageql_core import RunControl, RunLimits

from factory_agent.application.l2.service import SageAnalysisRunner
from factory_agent.application.usage import UsageContext, current_usage_context, set_usage_context
from factory_agent.domain import InteractionId, Role, SessionId
from factory_agent.execution.analysis import HostAnalysisExecutor
from factory_agent.execution.sandbox_runtime import InteractionSandbox
from factory_agent.ports import ModelRequest, ModelResponse, ModelStage, ModelUsage
from factory_agent.ports.analysis import AnalysisRequest
from factory_agent.ports.analysis_data import FactBatch, FactQuery
from factory_agent.ports.run_budget import consume_upstream_attempt
from tests.support.session import FrozenClock, SequentialIds
from tests.unit.execution.test_analysis_host import Dictionaries, Facts, host_for


class ObservationGateway:
    def __init__(
        self, mode: Literal["analysis", "knowledge", "clarify", "waiting", "repair"] = "analysis"
    ) -> None:
        self.mode = mode
        self.requests: list[ModelRequest] = []
        self.observations: list[list[dict[str, Any]]] = []
        self.started = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.started.set()
        if self.mode == "repair" and len(self.requests) == 1:
            return ModelResponse(
                '{"tool":"unknown-tool","arguments":{"raw_parameter":"synthetic-tool-parameter-canary"}}',
                "synthetic-planner",
                ModelUsage(4, 2),
            )
        if self.mode == "waiting":
            await asyncio.Event().wait()
        payload = cast("dict[str, Any]", json.loads(request.messages[-1].content))
        observations = cast("list[dict[str, Any]]", payload["observations"])
        self.observations.append(observations)
        if self.mode == "clarify":
            decision = {
                "tool": "finish_or_clarify",
                "arguments": {
                    "submission": {
                        "kind": "clarify",
                        "clarification": "请说明要按全部配置还是使用中配置统计。",
                    }
                },
            }
        elif self.mode in {"knowledge", "repair"}:
            decision = (
                {"tool": "lookup_knowledge", "arguments": {"query": "工序和车种有什么区别"}}
                if not observations
                else {
                    "tool": "finish_or_clarify",
                    "arguments": {
                        "submission": {
                            "kind": "knowledge",
                            "knowledge_refs": observations[-1]["knowledge_refs"],
                        }
                    },
                }
            )
        else:
            turn = len(observations)
            query = {
                "dataset_id": "reported_production",
                "columns": ["quantity", "plan_code", "process_id", "business_date", "bed_code"],
            }
            if turn in {0, 2}:
                decision = {"tool": "ensure_dataset", "arguments": {"query": query}}
            elif turn in {1, 3}:
                decision = {
                    "tool": "analyze_dataset",
                    "arguments": {
                        "plan": {
                            "dataset_refs": observations[-1]["dataset_refs"],
                            "group_by": ["plan_code"] if turn == 1 else ["process_id"],
                            "metrics": [{"metric_id": "quantity", "operation": "sum"}],
                        }
                    },
                }
            else:
                decision = {
                    "tool": "finish_or_clarify",
                    "arguments": {
                        "submission": {
                            "kind": "result",
                            "result_refs": [
                                observations[1]["result_refs"][0],
                                observations[3]["result_refs"][0],
                            ],
                        }
                    },
                }
        return ModelResponse(
            json.dumps(decision), "synthetic-planner", ModelUsage(4, 2), duration_ms=1
        )


def request_with(control: RunControl | None = None) -> AnalysisRequest:
    host = host_for(role=Role.OWNER)
    request = replace(host.request, run_control=control)
    host.close()
    host.sandbox.close()
    return request


def runner(
    facts: Facts, dictionaries: Dictionaries, gateway: ObservationGateway
) -> SageAnalysisRunner:
    return SageAnalysisRunner(
        facts,
        dictionaries,
        gateway,
        FrozenClock(),
        model_alias="factory-fast",
        new_id=SequentialIds("analysis"),
        sandbox_factory=InteractionSandbox,
        executor_factory=HostAnalysisExecutor,
    )


@pytest.mark.asyncio
async def test_real_runner_plans_from_observations_reuses_one_handle_and_runs_two_analyses() -> (
    None
):
    facts = Facts(
        (
            {
                "plan_code": "PLAN-A",
                "bed_code": "1555",
                "business_date": "2026-09-10",
                "process_id": "P1",
                "quantity": "2",
            },
            {
                "plan_code": "PLAN-A",
                "bed_code": "1555",
                "business_date": "2026-09-11",
                "process_id": "P2",
                "quantity": "3",
            },
            {
                "plan_code": "PLAN-B",
                "bed_code": "01555",
                "business_date": "2026-09-11",
                "process_id": "P2",
                "quantity": "999",
            },
        )
    )
    directories = Dictionaries()
    gateway = ObservationGateway()
    control = RunControl(time.monotonic() + 60)
    control.consume("model_rounds")
    control.consume("model_tokens", 7)

    request = request_with(control)
    previous_context = current_usage_context()
    set_usage_context(
        UsageContext(
            request.context.tenant_id,
            request.context.user_id,
            SessionId("synthetic-sage-session"),
            InteractionId(request.interaction_id),
            "synthetic-trace",
        )
    )
    try:
        outcome = await runner(facts, directories, gateway).analyze(request)
    finally:
        set_usage_context(previous_context)

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.rows == (("PLAN-A", Decimal(5)),)
    assert outcome.result.totals == {"quantity_sum": Decimal(5)}
    assert len(outcome.result.detail_tables) == 1
    assert len(facts.calls) == 1
    assert directories.calls == []
    assert gateway.observations[1][0]["dataset_refs"] == gateway.observations[3][-1]["dataset_refs"]
    assert gateway.observations[3][-1]["state"] == "interaction_reuse"
    assert control.used("analyses") == 2
    assert control.used("model_rounds") == 6
    assert control.used("model_tokens") == 37
    assert control.used("mes_requests") == control.used("mes_pages") == 0
    assert [request.stage for request in gateway.requests] == [ModelStage.PLAN] * 5
    prompts = "\n".join(
        message.content for request in gateway.requests for message in request.messages
    )
    assert "PLAN-A" not in prompts
    assert "01555" not in prompts
    assert "worker-a" not in prompts
    assert "999" not in prompts
    assert outcome.result.api_call_count == 0
    assert "quantity_sum" not in json.dumps(gateway.observations)
    assert control.finished
    model_events = [
        event for event in outcome.usage_events if event.event_type == "llm_call_completed"
    ]
    assert len(model_events) == 5
    assert {event.payload["stage"] for event in model_events} == {"plan"}
    completion = next(
        event for event in outcome.usage_events if event.event_type == "analysis_completed"
    )
    assert completion.payload["analyses"] == 2
    assert completion.payload["handle_reuse"] == 1
    assert completion.payload["persistent_cache_hits"] == 1
    assert completion.payload["mes_business_requests"] == 0
    assert "PLAN-A" not in json.dumps(
        [event.payload for event in outcome.usage_events], default=str
    )


@pytest.mark.asyncio
async def test_event_sink_emits_only_safe_phases_and_never_tool_parameters() -> None:
    phases: list[str] = []

    async def receive(phase: str) -> None:
        phases.append(phase)

    request = replace(request_with(), emit_phase=receive)
    outcome = await runner(Facts(), Dictionaries(), ObservationGateway("repair")).analyze(request)
    assert outcome.status == "knowledge"
    assert set(phases) == {"plan", "repair", "tool"}
    assert all(isinstance(phase, str) for phase in phases)
    assert "synthetic-tool-parameter-canary" not in json.dumps(phases)
    assert "query" not in json.dumps(phases)


@pytest.mark.asyncio
async def test_failing_event_callback_does_not_change_business_answer_or_leak_canary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    phases: list[str] = []

    async def failed_callback(phase: str) -> None:
        phases.append(phase)
        raise RuntimeError("synthetic-event-callback-canary")

    outcome = await runner(Facts(), Dictionaries(), ObservationGateway("knowledge")).analyze(
        replace(request_with(), emit_phase=failed_callback)
    )
    assert outcome.status == "knowledge"
    assert outcome.answer is not None and "车种" in outcome.answer
    assert set(phases) <= {"plan", "tool", "repair"}
    assert "synthetic-event-callback-canary" not in caplog.text
    assert "synthetic-event-callback-canary" not in outcome.answer


@pytest.mark.asyncio
async def test_real_runner_knowledge_and_specific_clarification_require_no_fact_fetch() -> None:
    facts = Facts()
    directories = Dictionaries()
    knowledge = await runner(facts, directories, ObservationGateway("knowledge")).analyze(
        replace(request_with(), time_range=None)
    )
    assert knowledge.status == "knowledge"
    assert knowledge.answer is not None and "车种" in knowledge.answer
    assert "docs/product/AI问答对外接口-整理.md" in knowledge.answer
    clarification = await runner(facts, directories, ObservationGateway("clarify")).analyze(
        request_with()
    )
    assert clarification.status == "clarify"
    assert clarification.answer == "请说明要按全部配置还是使用中配置统计。"
    assert facts.calls == []
    assert directories.calls == []


class BudgetedFacts(Facts):
    async def fetch(self, query: FactQuery) -> FactBatch:
        consume_upstream_attempt()
        return await super().fetch(query)


@pytest.mark.asyncio
async def test_real_runner_preserves_exhausted_upstream_budget_and_performs_zero_new_calls() -> (
    None
):
    facts = BudgetedFacts()
    control = RunControl(time.monotonic() + 60, RunLimits(max_mes_requests=1))
    control.consume("mes_requests")
    gateway = ObservationGateway()

    outcome = await runner(facts, Dictionaries(), gateway).analyze(request_with(control))

    assert outcome.status == "stopped"
    assert outcome.reason == "budget_exhausted"
    assert facts.calls == []
    assert control.used("mes_requests") == 1
    assert control.used("model_rounds") == 1
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_real_runner_cancels_a_waiting_planner_without_any_dataset_fetch() -> None:
    cancelled = asyncio.Event()
    control = RunControl(time.monotonic() + 60, cancelled=cancelled.is_set)
    facts = Facts()
    directories = Dictionaries()
    gateway = ObservationGateway("waiting")
    task = asyncio.create_task(
        runner(facts, directories, gateway).analyze(
            replace(request_with(control), cancelled=cancelled.is_set)
        )
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=2)
    cancelled.set()
    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome.status == "stopped"
    assert outcome.reason == "cancelled"
    assert facts.calls == []
    assert directories.calls == []
    assert control.finished
