import json
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from factory_agent.application.intent import CapabilityIntentParser
from factory_agent.application.session import SessionService, StartRequest
from factory_agent.domain import CapabilityId, InteractionStatus, Role, SessionId
from factory_agent.ports.analysis import AnalysisOutcome, AnalysisRequest
from factory_agent.ports.session import CapabilityRunResult
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
    SequentialIds,
)
from tests.unit.application.test_session_service import (
    CATALOG,
    NOW,
    authorization,
    credential,
    drain,
)


@dataclass
class RecordingAnalysis:
    outcome: AnalysisOutcome
    fact_dataset: str | None = None
    requests: list[AnalysisRequest] = field(default_factory=lambda: [])
    fact_calls: int = 0
    phases: tuple[str, ...] = ()

    async def analyze(self, request: AnalysisRequest) -> AnalysisOutcome:
        self.requests.append(request)
        assert await request.validate_active()
        if self.phases:
            assert request.emit_phase is not None
            for phase in self.phases:
                await request.emit_phase(phase)
        if self.fact_dataset is not None:
            assert request.validate_fact_scope is not None
            if not await request.validate_fact_scope(self.fact_dataset):
                return AnalysisOutcome("denied", reason="denied")
            self.fact_calls += 1
        return self.outcome


def build_service(
    analysis: RecordingAnalysis,
    slots: dict[str, object],
    *,
    role: Role = Role.EMPLOYEE,
    scope: str = "within",
    selected_capability: str = "l2_analysis",
    subject: str | None = None,
) -> tuple[SessionService, InMemoryInteractionStore, RecordingCapabilityRunner]:
    gateway = ScriptedModelGateway(
        contents=[
            json.dumps(
                {
                    "type": "l2" if selected_capability == "l2_analysis" else "capability",
                    "capability_id": selected_capability,
                    "confidence": 0.99,
                    "slots": slots,
                    "scope": {"verdict": scope, "target": ""},
                    "subject": subject,
                }
            )
        ]
    )
    store = InMemoryInteractionStore()
    deterministic = RecordingCapabilityRunner()
    service = SessionService(
        store,
        authorization(role),
        CapabilityIntentParser(
            gateway, CATALOG, model_alias="factory-fast", timezone_name="Asia/Shanghai"
        ),
        deterministic,
        FrozenClock(NOW),
        new_id=SequentialIds(),
        analysis_runner=analysis,
    )
    return service, store, deterministic


def result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("l2_analysis"),
        column_names=("quantity_sum",),
        rows=((Decimal(3),),),
        totals={"quantity_sum": Decimal(3)},
        source_operations=("reported_production",),
        column_titles={"quantity_sum": "报工量"},
        column_types={"quantity_sum": "quantity"},
        column_units={"quantity_sum": "件·工序"},
    )


@pytest.mark.asyncio
async def test_bed_query_routes_once_to_l2_and_preserves_self_scope_and_shared_budget() -> None:
    analysis = RecordingAnalysis(
        AnalysisOutcome("completed", result=result(), answer="报工量 3 件·工序。"),
        "reported_production",
    )
    service, store, deterministic = build_service(
        analysis,
        {"time_expression": "上个月", "bed_codes": ["01555"]},
        selected_capability="FR-001",
    )
    record = await service.start(
        credential(), StartRequest(SessionId("l2-session"), "上个月床号 01555 报工多少")
    )
    events = await drain(service, record.interaction_id)
    assert deterministic.requests == []
    assert analysis.fact_calls == 1
    request = analysis.requests[0]
    assert request.filters is not None
    assert request.filters.employee_ids == request.scope.employee_ids
    assert request.filters.bed_codes == frozenset({"01555"})
    assert request.run_control is not None
    assert request.deadline == request.run_control.deadline
    assert request.time_range is not None
    assert request.slots.bed_codes == ("01555",)
    assert events[-1].name == "interaction.completed"
    result_event = next(event for event in events if event.name == "interaction.result")
    assert result_event.data["row_count"] == 1
    assert result_event.data["answer"] == "报工量 3 件·工序。"
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED


@pytest.mark.asyncio
async def test_analysis_phases_persist_using_existing_progress_and_single_terminal() -> None:
    analysis = RecordingAnalysis(
        AnalysisOutcome("knowledge", answer="受审工序解释。"),
        phases=("plan", "tool", "repair", "untrusted-phase-canary"),
    )
    service, store, _deterministic = build_service(analysis, {})
    record = await service.start(
        credential(), StartRequest(SessionId("phase-session"), "工序是什么意思")
    )
    events = await drain(service, record.interaction_id)
    progress = [event for event in events if event.name == "interaction.progress"]
    rendered = json.dumps([event.data for event in progress], ensure_ascii=False)
    assert "analysis_plan" in rendered
    assert "analysis_tool" in rendered
    assert "analysis_repair" in rendered
    assert "untrusted-phase-canary" not in rendered
    stored = store.events[str(record.interaction_id)]
    assert all(event in stored for event in progress)
    assert [
        event.name
        for event in events
        if event.name in {"interaction.completed", "interaction.failed", "interaction.cancelled"}
    ] == ["interaction.completed"]


@pytest.mark.asyncio
async def test_knowledge_without_time_or_fact_permission_completes_with_zero_fact_calls() -> None:
    analysis = RecordingAnalysis(
        AnalysisOutcome("knowledge", answer="工序表示配置的生产步骤。依据：受审字段定义。")
    )
    service, store, deterministic = build_service(analysis, {}, scope="beyond")
    record = await service.start(
        credential(), StartRequest(SessionId("knowledge-session"), "工序是什么意思")
    )
    events = await drain(service, record.interaction_id)
    assert analysis.fact_calls == 0
    assert deterministic.requests == []
    assert analysis.requests[0].time_range is None
    assert events[-1].name == "interaction.completed"
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED
    assert (
        next(event for event in events if event.name == "interaction.result").data["answer"]
        == analysis.outcome.answer
    )


@pytest.mark.asyncio
async def test_beyond_scope_fact_query_is_denied_before_business_data_is_fetched() -> None:
    analysis = RecordingAnalysis(
        AnalysisOutcome("completed", result=result()), "reported_production"
    )
    service, store, deterministic = build_service(
        analysis, {"time_expression": "上个月"}, scope="beyond"
    )
    record = await service.start(
        credential(), StartRequest(SessionId("denied-session"), "全厂上个月报工量多少")
    )
    events = await drain(service, record.interaction_id)
    assert analysis.fact_calls == 0
    assert deterministic.requests == []
    assert events[-1].name == "interaction.failed"
    assert (
        store.interactions[str(record.interaction_id)].error_category == "analysis_scope_forbidden"
    )
    assert not any(event.name == "interaction.result" for event in events)


@pytest.mark.asyncio
async def test_agent_clarification_remains_a_completed_clarification_turn() -> None:
    analysis = RecordingAnalysis(AnalysisOutcome("clarify", answer="请提供要统计的制单日期窗口。"))
    service, store, deterministic = build_service(analysis, {})
    record = await service.start(
        credential(), StartRequest(SessionId("clarify-session"), "这个床配置多少工序")
    )
    events = await drain(service, record.interaction_id)
    assert deterministic.requests == []
    assert analysis.fact_calls == 0
    assert (
        next(event for event in events if event.name == "interaction.clarification").data[
            "question"
        ]
        == analysis.outcome.answer
    )
    assert events[-1].name == "interaction.completed"
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancelled_l2_interaction_stays_terminal_and_never_enters_analysis() -> None:
    analysis = RecordingAnalysis(
        AnalysisOutcome("completed", result=result()), "reported_production"
    )
    service, store, deterministic = build_service(analysis, {"time_expression": "上个月"})
    record = await service.start(
        credential(), StartRequest(SessionId("cancel-session"), "上个月床号产量多少")
    )
    await service.cancel(credential(), record.interaction_id)
    events = await drain(service, record.interaction_id)
    assert [event.name for event in events] == ["interaction.cancelled"]
    assert analysis.requests == []
    assert deterministic.requests == []
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.CANCELLED


@pytest.mark.parametrize("role", [Role.MANAGER, Role.OWNER])
@pytest.mark.parametrize("selected_capability", ["FR-001", "l2_analysis"])
@pytest.mark.asyncio
async def test_personal_queries_remain_self_scoped_for_wider_roles(
    role: Role, selected_capability: str
) -> None:
    analysis = RecordingAnalysis(
        AnalysisOutcome("completed", result=result()), "reported_production"
    )
    service, _store, deterministic = build_service(
        analysis,
        {"time_expression": "上个月", "bed_codes": ["1555"]},
        role=role,
        selected_capability=selected_capability,
        subject="self" if selected_capability == "l2_analysis" else None,
    )
    record = await service.start(
        credential(), StartRequest(SessionId("personal-l2"), "按工序看上个月床号 1555 产量")
    )
    events = await drain(service, record.interaction_id)
    request = analysis.requests[0]
    assert request.self_only
    assert request.filters is not None
    assert request.filters.employee_ids == request.scope.employee_ids
    assert request.filters.bed_codes == frozenset({"1555"})
    assert events[-1].name == "interaction.completed"
    assert deterministic.requests == []
