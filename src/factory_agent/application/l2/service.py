import time
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4
from zoneinfo import ZoneInfo

from sageql_core import (
    Agent,
    AnalysisExecutor,
    DataTools,
    NormalizedEvent,
    RunContext,
    RunControl,
    RunLimits,
)

from factory_agent.application.l2.model import PlanningModel
from factory_agent.application.l2.provider import (
    AnalysisHost,
    HostDatasetProvider,
    HostObservationPolicy,
)
from factory_agent.application.permission_matrix import ROLE_DATA_RANGE
from factory_agent.application.summary import format_aggregate_value
from factory_agent.application.usage import current_usage_context
from factory_agent.domain import CapabilityId
from factory_agent.observability.redaction import redact_text
from factory_agent.ports import Clock, ModelGateway, UsageEvent
from factory_agent.ports.analysis import AnalysisOutcome, AnalysisRequest
from factory_agent.ports.analysis_catalogue import KNOWLEDGE
from factory_agent.ports.analysis_data import FactSource
from factory_agent.ports.card import CardColumn, CardTableSpec, build_card
from factory_agent.ports.contracts import RenderColumn, RenderTable
from factory_agent.ports.dictionary import DictionarySource
from factory_agent.ports.result_table import ResultTable
from factory_agent.ports.run_budget import bind_run_budget
from factory_agent.ports.sandbox import InteractionSandboxPort
from factory_agent.ports.session import CapabilityRunResult


def _render_table(table: ResultTable) -> RenderTable:
    return RenderTable(
        capability_id=table.capability_id,
        columns=tuple(
            RenderColumn(
                column.name,
                column.metric_name,
                column.metric_version,
                column.source_operations,
                column.column_type,
                column.unit,
                column.title,
            )
            for column in table.columns
        ),
        rows=table.rows,
        totals=table.totals,
        source_operations=table.source_operations,
        warnings=table.warnings,
        incomplete=table.incomplete,
        incomplete_reason=table.incomplete_reason,
        hidden_columns=table.hidden_columns,
    )


class ReviewedKnowledgeProvider:
    def __init__(self, host: AnalysisHost) -> None:
        self.host = host

    async def lookup(self, context: RunContext, query: str, control: RunControl) -> object:
        await self.host.validate(context, control)
        selected = tuple(
            piece
            for piece in KNOWLEDGE
            if any(
                word in query and word in piece.text
                for word in (
                    "床号",
                    "报工",
                    "产量",
                    "制单",
                    "工序",
                    "车种",
                    "类型",
                    "配置",
                    "尺码",
                    "权限",
                )
            )
        )
        if not selected:
            selected = KNOWLEDGE
        references = tuple(self.host.register_knowledge(piece) for piece in selected)
        return self.host.observation(
            state="knowledge", knowledge_refs=references, knowledge=selected, source="knowledge"
        )


class InteractionEventSink:
    def __init__(self, request: AnalysisRequest) -> None:
        self.request = request

    async def emit(self, event: NormalizedEvent) -> None:
        if event.kind == "phase" and self.request.emit_phase is not None:
            await self.request.emit_phase(event.phase)


class SageAnalysisRunner:
    def __init__(
        self,
        facts: FactSource,
        dictionaries: DictionarySource,
        model: ModelGateway,
        clock: Clock,
        *,
        model_alias: str,
        sandbox_factory: Callable[[], InteractionSandboxPort],
        executor_factory: Callable[[AnalysisHost], AnalysisExecutor],
        limits: RunLimits | None = None,
        factory_timezone: str = "Asia/Shanghai",
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._facts = facts
        self._dictionaries = dictionaries
        self._model = model
        self._clock = clock
        self._alias = model_alias
        self._limits = limits or RunLimits()
        self._zone = ZoneInfo(factory_timezone)
        self._new_id = new_id or (lambda: uuid4().hex)
        self._sandbox_factory = sandbox_factory
        self._executor_factory = executor_factory

    async def analyze(self, request: AnalysisRequest) -> AnalysisOutcome:
        started = time.monotonic()
        events: list[UsageEvent] = []
        if not await request.validate_active():
            return AnalysisOutcome("denied", reason="denied")
        control = request.run_control or RunControl(
            request.deadline, self._limits, request.cancelled
        )
        control.restrict(self._limits)
        sandbox = self._sandbox_factory()
        host = AnalysisHost(request, self._facts, self._dictionaries, sandbox, new_id=self._new_id)
        tools = DataTools(
            HostDatasetProvider(host),
            ReviewedKnowledgeProvider(host),
            self._executor_factory(host),
            HostObservationPolicy(host),
        )
        model = PlanningModel(
            self._model, self._clock, control, events, model_alias=self._alias, new_id=self._new_id
        )
        question = redact_text(request.question)
        for name in request.slots.employee_names:
            question = question.replace(name, "所指定的获准人员")
        try:
            with bind_run_budget(control):
                run = await Agent(model, tools, InteractionEventSink(request)).run(
                    question, RunContext(host), control
                )
            observations = run.observations
            counts = {
                "model_rounds": control.used("model_rounds"),
                "tool_steps": control.used("tool_steps"),
                "analyses": control.used("analyses"),
                "mes_requests": control.used("mes_requests"),
                "mes_business_requests": control.used("mes_pages"),
                "processed_rows": control.used("processed_rows"),
                "model_tokens": control.used("model_tokens"),
                "semantic_repairs": control.used("semantic_repairs"),
                "handle_reuse": sum(item.state == "interaction_reuse" for item in observations),
                "persistent_cache_hits": sum(item.source == "cache" for item in observations),
                "logical_loads": sum(item.source in {"cache", "upstream"} for item in observations),
                "complete_cache_coverage": int(
                    run.status == "completed"
                    and run.submission is not None
                    and run.submission.kind == "result"
                    and bool(host.datasets)
                    and all(
                        item.source != "upstream"
                        and item.state not in {"unavailable", "incomplete", "invalid_action"}
                        for item in observations
                    )
                ),
            }
            context = current_usage_context()
            if context is not None:
                payload = context.envelope("analysis_completed", self._clock.now())
                payload.update({"status": run.status, "reason_code": run.reason_code, **counts})
                events.append(
                    UsageEvent(
                        self._new_id(),
                        "analysis_completed",
                        context.tenant_id,
                        payload,
                        self._clock.now(),
                    )
                )
            submission = run.submission
            if run.status == "completed" and submission is not None:
                if submission.kind == "knowledge":
                    pieces = tuple(
                        host.knowledge[reference] for reference in submission.knowledge_refs
                    )
                    answer = "\n\n".join(
                        f"{piece.text}\n依据：{piece.source}（{piece.version}）。"
                        for piece in pieces
                    )
                    return AnalysisOutcome("knowledge", answer=answer, usage_events=tuple(events))
                if submission.kind == "result":
                    tables = tuple(
                        host.get_result(reference) for reference in submission.result_refs
                    )
                    converted = tuple(
                        self._result(table, counts, started, request) for table in tables
                    )
                    primary = converted[0]
                    if len(converted) > 1:
                        primary = replace(
                            primary,
                            detail_tables=tuple(_render_table(table) for table in tables[1:]),
                        )
                    answer = self._answer(primary, request)
                    return AnalysisOutcome(
                        "completed", result=primary, answer=answer, usage_events=tuple(events)
                    )
            if run.status == "clarify":
                return AnalysisOutcome(
                    "clarify",
                    answer=redact_text(submission.clarification)
                    if submission is not None and submission.clarification
                    else "请补充要统计的时间范围、目标或配置使用状态。",
                    reason=run.reason_code,
                    usage_events=tuple(events),
                )
            return AnalysisOutcome(run.status, reason=run.reason_code, usage_events=tuple(events))
        finally:
            host.close()
            sandbox.close()

    @staticmethod
    def _result(
        table: ResultTable, counts: dict[str, int], started: float, request: AnalysisRequest
    ) -> CapabilityRunResult:
        names = tuple(column.name for column in table.columns)
        card = build_card(
            CardTableSpec("table", totals=tuple(table.totals), preview_max_rows=100),
            capability_id="l2_analysis",
            title="自主分析结果",
            columns=tuple(
                CardColumn(column.name, column.title, column.column_type, column.unit)
                for column in table.columns
            ),
            rows=table.rows,
            totals=table.totals,
            warnings=table.warnings,
        )
        return CapabilityRunResult(
            capability_id=CapabilityId("l2_analysis"),
            column_names=names,
            rows=tuple(tuple(row.get(name) for name in names) for row in table.rows),
            totals=table.totals,
            source_operations=table.source_operations,
            column_types={column.name: column.column_type or "text" for column in table.columns},
            column_units={column.name: column.unit or "" for column in table.columns},
            column_titles={column.name: column.title or column.name for column in table.columns},
            warnings=table.warnings,
            card=card,
            cache_source={
                "analysis": counts,
                "scope_state": "self"
                if request.self_only
                else "mes_filtered"
                if any(
                    dataset in {"reported_production", "production_progress"}
                    for dataset in table.source_operations
                )
                else "authorized",
            },
            api_call_count=counts["mes_business_requests"],
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    def _answer(self, result: CapabilityRunResult, request: AnalysisRequest) -> str:
        data_range = (
            ROLE_DATA_RANGE[request.context.role]
            if any(
                dataset in {"reported_production", "production_progress"}
                for dataset in result.source_operations
            )
            else "当前租户配置"
        )
        if request.self_only and any(
            dataset in {"reported_production", "production_progress"}
            for dataset in result.source_operations
        ):
            data_range = "本人数据"
        lines = [f"已在{data_range}范围内返回 {len(result.rows)} 行结果。"]
        if request.time_range is not None:
            lines.append(
                "用户请求的事实窗口为半开区间 "
                + request.time_range.start.astimezone(self._zone).isoformat()
                + " 至 "
                + request.time_range.end.astimezone(self._zone).isoformat()
                + "。"
            )
        if request.slots.bed_codes:
            lines.append("床号精确条件：" + "、".join(request.slots.bed_codes) + "。")
        for name, value in result.totals.items():
            title = (result.column_titles or {}).get(name, name)
            unit = (result.column_units or {}).get(name, "")
            formatted = format_aggregate_value(value, (result.column_types or {}).get(name))
            lines.append(f"{title}：{formatted}{unit}。")
        lines.extend(result.warnings)
        if not result.rows:
            lines.append("完整窗口内没有匹配记录，不代表床号、订单或配置不存在。")
        if "style_processes" in result.source_operations:
            lines.append("配置种类数按非空工序编码去重，配置记录数另计。")
        if "style_sizes" in result.source_operations:
            lines.append("配置尺码保留原始标识及版型，不代表本窗口实际生产过的尺码。")
        return "\n".join(lines)
