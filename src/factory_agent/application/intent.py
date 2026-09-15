"""Capability selection, slot extraction, and bounded clarification.

The parser replaces the report-agent free-form ``DraftFilterSpec`` with a typed
``CapabilityIntent`` restricted to registered capabilities. It never accepts
employee or department identifiers from the user or the model: those come only
from the trusted ``DataScope``. A low-confidence or incomplete result produces a
short clarification question instead of an unbounded query.
"""



import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from factory_agent.application.context import ConversationTurn, compact_history
from factory_agent.application.scope_guard import (
    ScopeClassification,
    merged_scope_block,
    parse_scope_classification,
)
from factory_agent.application.structured import (
    StructuredOutputError,
    request_structured_object,
)
from factory_agent.application.time_expressions import (
    DEFAULT_TIME_RANGE_MAX_DAYS,
    TimeExpressionError,
    resolve_time_expression,
    time_range_violation,
)
from factory_agent.domain import (
    CapabilityId,
    CapabilityIntent,
    IntentSlots,
    Role,
    TimeRange,
)
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports import ModelGateway, ModelMessage, ModelRequest, ModelStage

_LOGGER = get_logger("factory_agent.application.intent")

MIN_CAPABILITY_CONFIDENCE = 0.6

#: Slot names the model is allowed to fill. Scope identifiers are absent by design.
ALLOWED_SLOT_NAMES: frozenset[str] = frozenset(
    {
        "time_expression",
        "time_range_start",
        "time_range_end",
        "order_codes",
        "plan_codes",
        "style_codes",
        "material_ids",
        "dept_names",
        "employee_names",
    }
)

#: Slot names that must never reach the executor even if a model emits them.
REJECTED_SLOT_NAMES: frozenset[str] = frozenset(
    {"employee_ids", "dept_ids", "tenant_id", "user_id", "scope", "sql", "url"}
)

#: Chinese labels used when describing required slots in the capability list.
#: The JSON slot keys the model emits stay English; these labels only make the
#: prompt readable so the model can judge what information is still missing.
_SLOT_LABELS_CN: dict[str, str] = {
    "time_range": "时间范围",
    "order_codes": "订单号",
    "plan_codes": "计划单号",
    "style_codes": "款号",
    "material_ids": "物料编号",
    "dept_names": "车间或组别",
    "employee_names": "员工姓名",
}

_CLARIFICATION_PROMPTS: dict[str, str] = {
    "time_range": "请补充时间范围，例如“本月”“上周”或“2026-08”。",
    "order_codes": "请提供具体的订单号。",
    "plan_codes": "请提供具体的计划单号。",
    "style_codes": "请提供具体的款号。",
    "material_ids": "请提供具体的物料编号（缝制生产进度详情里的物料编号）。",
    "dept_names": "请说明要看哪个车间或组别。",
    "employee_names": "请说明要看哪位员工。",
}

_CAPABILITY_CLARIFICATION = "没有识别出可执行的查询，请说明您想查看的业务内容。"
_MAX_LIST_ITEMS = 20
_MAX_CODE_CHARS = 64


class ClarificationLimitError(Exception):
    """Raised when the configured clarification round budget is exhausted."""

    def __init__(self, rounds: int) -> None:
        super().__init__(f"clarification budget of {rounds} rounds is exhausted")
        self.rounds = rounds


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_id: CapabilityId
    title: str
    description: str = ""
    required_slots: tuple[str, ...] = ()
    #: Roles allowed to select this capability; empty means every role, which
    #: covers chit-chat and catalogs assembled directly in tests.
    roles: frozenset[Role] = frozenset()

    def selectable_by(self, role: Role | None) -> bool:
        """Whether one caller role may select this capability.

        ``None`` means the caller role is unknown (tests, offline evaluation),
        and the spec stays selectable so those callers are unaffected.
        """
        if role is None or not self.roles:
            return True
        return role in self.roles


@dataclass(frozen=True, slots=True)
class CapabilityCatalog:
    """Registered capabilities the parser is allowed to select."""

    specs: tuple[CapabilitySpec, ...] = ()

    def get(self, capability_id: str) -> CapabilitySpec | None:
        for spec in self.specs:
            if spec.capability_id == capability_id:
                return spec
        return None

    def describe(self, role: Role | None = None) -> str:
        """The capability list handed to the selector model.

        ``role`` restricts the list to the capabilities that caller may
        actually select, so the model cannot route a request to a capability
        the role matrix would deny. ``None`` keeps the full list, which is what
        tests and the offline evaluation use.
        """
        lines: list[str] = []
        for spec in self.specs:
            if not spec.selectable_by(role):
                continue
            line = f"- {spec.capability_id}（{spec.title}）"
            if spec.description:
                line = f"{line}：{spec.description}"
            if spec.required_slots:
                labels = "、".join(_SLOT_LABELS_CN.get(name, name) for name in spec.required_slots)
                line = f"{line}（需要提供：{labels}）"
            lines.append(line)
        return "\n".join(lines)


SYSTEM_PROMPT = (
    "你是工厂业务助手的能力选择器与追问改写器，只能从给定的能力列表中选择一个 capability_id。\n"
    "输出类型 type：\n"
    "1. 用户问候、寒暄，或问与工厂业务无关的常识问题（如人物介绍、天气常识）时，"
    'type 填 "chitchat"，capability_id 填 "chitchat"，并在 content 中直接生成'
    "可回复用户的简洁中文答案（一般不超过 200 字）。闲聊回复不得编造任何工厂/MES 的"
    "生产、产量或工资数据；涉及实时数据或无法核实的信息（如今天天气、实时股价）时，"
    "如实说明无法获取，不要猜测。\n"
    "2. 用户询问产量、工资、订单/款号进度、车间对比、排名等工厂业务时，"
    'type 填 "capability"，content 填空字符串，按下面的业务规则选择能力。\n'
    "多轮改写 rewrite_query：\n"
    "3. 当消息列表里已有历史对话（多轮）时，若当前问题是省略式/指代式追问"
    "（如“那…呢”“这个/它/再呢”“上月呢”这类缺少主语或宾语、依赖上一轮的表述），"
    "必须结合历史把它改写成语义完整、可独立理解的一句话填入 rewrite_query，"
    "并按改写后的含义选择 capability_id 与 slots（可沿用上一轮的话题与已给条件，"
    "例如上一轮查了某员工工资，本轮“那这个月呢”仍指该员工与本话题）。"
    "不要臆造历史中不存在的员工、款号或时间。\n"
    "4. 当没有历史对话（首轮）时，rewrite_query 填当前问题即可。\n"
    "业务能力选择：\n"
    "5. 选中的业务能力缺少必填条件（如时间范围、订单号）时，把对应槽位留空，"
    "不要臆造默认时间（如“本月”“今天”），由系统追问补全。\n"
    "6. 无法判断应归属哪个能力时，capability_id 设为 null。\n"
    "不得发明新的能力，不得输出 SQL、URL、员工编号或部门编号。\n"
    "时间解析（slots 的时间字段）：\n"
    "7. 已知具体日期时，把区间换算成 ISO 8601 填入 time_range_start / time_range_end，"
    "按半开区间 [start, end) 表示，end 取区间结束日的次日零点；同时把用户原话填进"
    "time_expression 便于核对。\n"
    "8. 相对表达一律以系统给出的“今天是 …”为基准换算（如“本月”填本月一日零点到"
    "次月一日零点，“昨天”填昨天零点到今天零点）。\n"
    "9. 不在常见词表里的表达，只要按语义能定出绝对区间也要换算（如“6月15号”"
    "“上个季度”）。\n"
    "10. 问题没有提到任何时间时，time_expression、time_range_start、time_range_end "
    "三个字段都留空，由系统追问补全，不要臆造默认时间。\n"
    "严格输出一个 JSON 对象：\n"
    '{"type": "chitchat" 或 "capability", "content": "...", "rewrite_query": "...", '
    '"capability_id": "<列表中的 id 或 null>", "confidence": 0.0-1.0, '
    '"slots": {"time_expression": "...", "time_range_start": null, '
    '"time_range_end": null, "order_codes": [], "plan_codes": [], "style_codes": [], '
    '"material_ids": [], "dept_names": [], "employee_names": []}, "ambiguous": []}\n'
    "不要输出解释或代码块。"
)


def _today_line(now: datetime, tz_name: str) -> str:
    """Clock anchor for relative time expressions, in the factory timezone."""
    local = now.astimezone(ZoneInfo(tz_name))
    return f"今天是 {local.date().isoformat()}（{tz_name}）。"


#: 「产量」类问法在两个能力之间的归属规则。Both capabilities answer a bare
#: "车间产量" question, so without this rule the selector model can only go by
#: wording similarity and picks the comparison one.
WORKSHOP_OUTPUT_ROUTING_RULE = (
    "产量类问法的能力归属（硬规则）：\n"
    "- 用户只问产量多少、做了多少件，而没有提到对比、排名、名次、谁高谁低、人均时，"
    "选 fr010_workshop_output_overview（车间产量总览），它只给出产量本身；\n"
    "- 只有当用户明确要求对比、排名、名次、人均产量或各组之间比高低时，"
    "才选 fr007_workshop_output_comparison（小组/车间产量对比）。"
)

#: The two capabilities the routing rule disambiguates, in the rule's own order.
_ROUTED_CAPABILITY_IDS = (
    "fr010_workshop_output_overview",
    "fr007_workshop_output_comparison",
)


def workshop_output_routing_block(catalog: CapabilityCatalog, role: Role | None) -> str | None:
    """The routing rule, only for a caller that may select both capabilities.

    Withholding it elsewhere keeps the promise that the selector prompt never
    describes a capability its caller's role could not select.
    """
    for capability_id in _ROUTED_CAPABILITY_IDS:
        spec = catalog.get(capability_id)
        if spec is None or not spec.selectable_by(role):
            return None
    return WORKSHOP_OUTPUT_ROUTING_RULE


#: 进度 / 产量 / 下钻三层之间的归属规则。These five capabilities answer adjacent
#: questions about the same orders, so wording similarity alone cannot separate
#: them: "这个单做到哪了" is progress while "这个单做了多少" is output, and the
#: two drill-downs must only be chosen when the user asked for that grain *and*
#: supplied the key it needs (订单号 / 物料编号).
PROGRESS_FLOW_RULE_HEADER = (
    "进度与产量类问法的能力归属（硬规则）：\n"
    "- 先分辨用户问的是「进度」（做到哪了）还是「产量」（做了多少件）：进度选进度能力，"
    "产量选产量能力；两者都问时以进度能力为主。\n"
    "- 三层下钻（订单/款号列表 → 包级明细 → 工序明细）是**三次不同的提问**，"
    "只有用户明确要求下一层、且给出了该层需要的编号时才选更细一层的能力。"
)

#: One line per routed capability, filtered to what the role may actually
#: select. Each line names only its own capability (id + title), so no line can
#: leak a capability the caller would be denied.
_PROGRESS_FLOW_RULES: tuple[tuple[str, str], ...] = (
    (
        "fr005_order_progress",
        "- 问「做到哪了 / 什么进度 / 还差多少 / 完工了吗」，按订单号或款号看进度列表 → "
        "选 fr005_order_progress（订单/款号进度查询）。",
    ),
    (
        "fr009_factory_order_overview",
        "- 老板要看全厂所有订单的进度 → "
        "选 fr009_factory_order_overview（各订单进度（全厂总览））。",
    ),
    (
        "fr006_order_output",
        "- 问「做了多少件 / 产量多少 / 哪道工序做得多」→ "
        "选 fr006_order_output（订单/款号产量查询）。",
    ),
    (
        "fr005_order_package_detail",
        "- 用户点名某个订单「每个包 / 包级明细 / 各包完成情况」→ "
        "选 fr005_order_package_detail（订单进度-包级明细）。",
    ),
    (
        "fr005_order_worktype_detail",
        "- 用户点名某个包（给出物料编号）的「工序明细 / 这道包做到哪道工序」→ "
        "选 fr005_order_worktype_detail（订单进度-工序明细）；给不出物料编号就不要选它。",
    ),
)


def progress_flow_routing_block(catalog: CapabilityCatalog, role: Role | None) -> str | None:
    """The progress/output/drill-down routing rule for the capabilities in scope.

    Only the lines whose capability the caller may select are emitted, so a role
    is never told about a capability it would be denied, and the rule stays
    useful for partial catalogs (tests, offline evaluation).
    """
    lines = [
        line
        for capability_id, line in _PROGRESS_FLOW_RULES
        if (spec := catalog.get(capability_id)) is not None and spec.selectable_by(role)
    ]
    if not lines:
        return None
    return "\n".join((PROGRESS_FLOW_RULE_HEADER, *lines))


def build_intent_messages(
    user_text: str,
    catalog: CapabilityCatalog,
    history: tuple[ConversationTurn, ...] = (),
    *,
    max_turns: int,
    max_chars: int,
    today_line: str | None = None,
    scope_block: str | None = None,
    role: Role | None = None,
) -> tuple[ModelMessage, ...]:
    """Compose the EXTRACT request.

    ``scope_block`` adds the merged scope-judgement section for the caller's
    role. It is omitted entirely when the deployment runs the dedicated guard,
    so that mode sends a byte-identical prompt to the one already in service.

    ``role`` narrows the capability list to what the caller may select, and adds
    the workshop-output routing rule when that role may select both capabilities
    the rule chooses between, plus the progress/output/drill-down rule for the
    progress capabilities it may select. The scope section judges only the
    requested data range and never changes that selection.
    """
    system_content = f"{SYSTEM_PROMPT}\n\n可用能力:\n{catalog.describe(role)}"
    for block in (
        workshop_output_routing_block(catalog, role),
        progress_flow_routing_block(catalog, role),
    ):
        if block:
            system_content = f"{system_content}\n\n{block}"
    if today_line:
        system_content = f"{system_content}\n\n{today_line}"
    if scope_block:
        system_content = f"{system_content}\n\n{scope_block}"
    system = ModelMessage(role="system", content=system_content)
    compacted = compact_history(history, max_turns=max_turns, max_chars=max_chars)
    return (system, *compacted, ModelMessage(role="user", content=user_text))


@dataclass(frozen=True, slots=True)
class ParsedIntent:
    """Typed interpretation of one model payload, independent of any model call.

    ``content`` carries the chit-chat reply when the same call produced one
    (merged single-call path); ``rewrite_query`` carries the standalone query
    the model produced for a multi-turn follow-up, when one was needed.
    ``scope_verdict`` carries the scope classification when the same call was
    asked for one; ``None`` means the caller must fall back to the dedicated
    guard call, never that the request is within range.
    """

    intent: CapabilityIntent
    rejected_slots: tuple[str, ...] = ()
    content: str | None = None
    rewrite_query: str | None = None
    scope_verdict: ScopeClassification | None = None


@dataclass(frozen=True, slots=True)
class IntentParseOutcome:
    intent: CapabilityIntent
    clarification: str | None
    attempts: int
    actual_model: str
    duration_ms: int
    rejected_slots: tuple[str, ...] = ()
    content: str | None = None
    rewrite_query: str | None = None
    #: True when this call was asked to classify the scope as well, so the
    #: metering event can say so even when the model omitted the key.
    includes_scope: bool = False
    scope_verdict: ScopeClassification | None = None


@dataclass(frozen=True, slots=True)
class _ModelRange:
    """Outcome of judging the model's ISO time range against the redlines."""

    start: datetime | None = None
    end: datetime | None = None
    #: A real span beyond the ceiling: the session gate owns the user-facing
    #: notice, so the range is handed through rather than questioned again.
    oversize: tuple[datetime, datetime] | None = None
    #: The model produced a range that cannot be used and is not a ceiling
    #: case, so a time-range clarification is the honest answer.
    unusable: bool = False


class CapabilityIntentParser:
    """Turns one utterance into a typed ``CapabilityIntent``."""

    def __init__(
        self,
        gateway: ModelGateway,
        catalog: CapabilityCatalog,
        *,
        model_alias: str,
        timezone_name: str,
        max_repair_attempts: int = 1,
        max_history_turns: int = 8,
        max_history_chars: int = 32768,
        min_confidence: float = MIN_CAPABILITY_CONFIDENCE,
        time_range_max_days: int = DEFAULT_TIME_RANGE_MAX_DAYS,
        time_parse_mode: Literal["llm_primary", "rule_primary"] = "llm_primary",
        scope_guard_mode: Literal["merged", "dedicated"] = "merged",
    ) -> None:
        self._gateway = gateway
        self._catalog = catalog
        self._model_alias = model_alias
        self._timezone_name = timezone_name
        self._max_repair_attempts = max_repair_attempts
        self._max_history_turns = max_history_turns
        self._max_history_chars = max_history_chars
        self._min_confidence = min_confidence
        self._time_range_max_days = time_range_max_days
        self._time_parse_mode = time_parse_mode
        self._scope_guard_mode = scope_guard_mode

    def includes_scope_for(self, role: Role | None) -> bool:
        """Whether an EXTRACT call for this caller also carries the scope verdict.

        A call only carries it in ``merged`` mode and only when the caller's
        role is known, because the reviewed judgement rules are role-relative.
        """
        return self._scope_guard_mode == "merged" and role is not None

    async def parse(
        self,
        user_text: str,
        *,
        now: datetime,
        logical_call_id: str,
        history: tuple[ConversationTurn, ...] = (),
        role: Role | None = None,
    ) -> IntentParseOutcome:
        if not user_text.strip():
            raise StructuredOutputError("user text is empty", attempts=0)

        # The merged carrier needs the caller's role; without one (or with the
        # dedicated guard selected) the prompt asks for nothing extra and the
        # scope decision stays with the dedicated call.
        includes_scope = self.includes_scope_for(role)
        request = ModelRequest(
            model_alias=self._model_alias,
            messages=build_intent_messages(
                user_text,
                self._catalog,
                history,
                max_turns=self._max_history_turns,
                max_chars=self._max_history_chars,
                today_line=_today_line(now, self._timezone_name),
                scope_block=merged_scope_block(role) if includes_scope and role else None,
                role=role,
            ),
            stage=ModelStage.EXTRACT,
            logical_call_id=logical_call_id,
            json_output=True,
        )
        result = await request_structured_object(
            self._gateway, request, max_repair_attempts=self._max_repair_attempts
        )
        parsed = self.interpret(result.payload, now=now, with_scope=includes_scope, role=role)
        return IntentParseOutcome(
            intent=parsed.intent,
            clarification=clarification_for(parsed.intent),
            attempts=result.attempts,
            actual_model=result.response.actual_model,
            duration_ms=result.response.duration_ms,
            rejected_slots=parsed.rejected_slots,
            content=parsed.content,
            rewrite_query=parsed.rewrite_query,
            includes_scope=includes_scope,
            scope_verdict=parsed.scope_verdict,
        )

    def interpret(
        self,
        payload: dict[str, object],
        *,
        now: datetime,
        with_scope: bool = False,
        role: Role | None = None,
    ) -> ParsedIntent:
        """Validate a raw model payload into a typed intent.

        ``type`` discriminates chit-chat from business capability routing. The
        schema is additive and backward compatible: a legacy payload without
        ``type`` is still routed by ``capability_id == "chitchat"``, and an
        absent ``content``/``rewrite_query`` is simply ``None``. Chit-chat
        content is only ever accepted when the payload is a chit-chat. A
        ``scope`` value is read only when ``with_scope`` says this call was
        asked for it; otherwise it is ignored like any other unknown key.
        ``role`` restricts capability resolution to what that caller may
        select; without it every registered capability resolves.
        """
        ambiguous = list(_string_list(payload.get("ambiguous")))
        confidence = _confidence(payload.get("confidence"))
        scope_verdict = parse_scope_classification(payload.get("scope")) if with_scope else None
        raw_slots = payload.get("slots")
        slots_mapping: dict[str, Any] = (
            cast("dict[str, Any]", raw_slots) if isinstance(raw_slots, dict) else {}
        )
        rejected: tuple[str, ...] = tuple(
            sorted(name for name in slots_mapping if name in REJECTED_SLOT_NAMES)
        )
        rewrite_query = _trimmed_text(payload.get("rewrite_query"))
        is_chitchat = _is_chitchat(payload.get("type"), payload.get("capability_id"))

        if is_chitchat:
            spec = self._catalog.get("chitchat")
            if spec is None:
                ambiguous.append("capability")
                return ParsedIntent(
                    intent=CapabilityIntent(
                        capability_id=None,
                        confidence=confidence,
                        ambiguous=tuple(dict.fromkeys(ambiguous)),
                    ),
                    rejected_slots=rejected,
                    scope_verdict=scope_verdict,
                    rewrite_query=rewrite_query,
                )
            if confidence < self._min_confidence:
                ambiguous.append("capability")
            return ParsedIntent(
                intent=CapabilityIntent(
                    capability_id=spec.capability_id,
                    confidence=confidence,
                    ambiguous=tuple(dict.fromkeys(ambiguous)),
                ),
                rejected_slots=rejected,
                scope_verdict=scope_verdict,
                content=_trimmed_text(payload.get("content")),
                rewrite_query=rewrite_query,
            )

        spec = self._resolve_capability(payload.get("capability_id"), role)
        if spec is None:
            ambiguous.append("capability")
            return ParsedIntent(
                intent=CapabilityIntent(
                    capability_id=None,
                    confidence=confidence,
                    ambiguous=tuple(dict.fromkeys(ambiguous)),
                ),
                rejected_slots=rejected,
                scope_verdict=scope_verdict,
                rewrite_query=rewrite_query,
            )
        if confidence < self._min_confidence:
            ambiguous.append("capability")

        slots, slot_ambiguity = self._build_slots(slots_mapping, now=now)
        ambiguous.extend(slot_ambiguity)
        missing = tuple(name for name in spec.required_slots if name not in slots.filled_names())
        return ParsedIntent(
            intent=CapabilityIntent(
                capability_id=spec.capability_id,
                confidence=confidence,
                slots=slots,
                missing=missing,
                ambiguous=tuple(dict.fromkeys(ambiguous)),
            ),
            rejected_slots=rejected,
            scope_verdict=scope_verdict,
            rewrite_query=rewrite_query,
        )

    def _resolve_capability(self, raw: object, role: Role | None) -> CapabilitySpec | None:
        """Resolve one model-selected id against the caller's own catalog.

        A capability the caller's role cannot select never appears in that
        role's prompt, so the model can only produce one by echoing it from
        history. Treating it as unresolvable turns that into a clarification
        instead of a denial decided after the fact.
        """
        if not isinstance(raw, str) or not raw.strip():
            return None
        spec = self._catalog.get(raw.strip())
        if spec is None or not spec.selectable_by(role):
            return None
        return spec

    def _build_slots(
        self, raw: dict[str, Any], *, now: datetime
    ) -> tuple[IntentSlots, tuple[str, ...]]:
        ambiguous: list[str] = []
        expression = raw.get("time_expression")

        # The reviewed rule layer runs first: it is the fallback whenever the
        # model's ISO range is absent or fails a redline, and its resolution is
        # the counterpart the mismatch log compares against.
        rule_range: TimeRange | None = None
        rule_failed = False
        if isinstance(expression, str) and expression.strip():
            try:
                rule_range = resolve_time_expression(expression, now, self._timezone_name)
            except TimeExpressionError:
                rule_failed = True

        start: datetime | None = None
        end: datetime | None = None
        oversize: tuple[datetime, datetime] | None = None
        unusable = False
        if self._time_parse_mode == "llm_primary":
            model = self._model_range(raw, now=now, rule_range=rule_range)
            start, end = model.start, model.end
            oversize = model.oversize
            unusable = model.unusable

        if start is None or end is None:
            if rule_range is not None:
                start, end = rule_range.start, rule_range.end
            elif oversize is not None:
                # Nothing reviewed to fall back on and the requested span really
                # is explicit: keep it so the session's ceiling gate answers with
                # the friendly notice instead of asking the user again.
                start, end = oversize
            elif rule_failed or unusable:
                ambiguous.append("time_range")

        return (
            IntentSlots(
                time_range_start=start,
                time_range_end=end,
                time_expression=expression.strip() if isinstance(expression, str) else None,
                order_codes=_code_list(raw.get("order_codes")),
                plan_codes=_code_list(raw.get("plan_codes")),
                style_codes=_code_list(raw.get("style_codes")),
                material_ids=_code_list(raw.get("material_ids")),
                dept_names=_code_list(raw.get("dept_names")),
                employee_names=_code_list(raw.get("employee_names")),
            ),
            tuple(ambiguous),
        )

    def _model_range(
        self,
        raw: dict[str, Any],
        *,
        now: datetime,
        rule_range: TimeRange | None,
    ) -> _ModelRange:
        """Judge the model's ISO range against the shared redlines.

        The range is accepted when it clears every redline. A ``too_long`` span
        is held back as ``oversize`` for the session's ceiling gate, because an
        explicitly requested wide range deserves the friendly notice rather than
        a fresh question. Anything else implausible (inverted, starting after
        tomorrow) is marked ``unusable``, so a hallucinated span — "本月" read as
        two years — falls back to the reviewed rule layer instead of becoming a
        rejection.
        """
        start = _parse_datetime(raw.get("time_range_start"), self._timezone_name)
        end = _parse_datetime(raw.get("time_range_end"), self._timezone_name)
        if start is None or end is None:
            return _ModelRange()
        reason = time_range_violation(
            start,
            end,
            max_days=self._time_range_max_days,
            now=now,
            tz_name=self._timezone_name,
        )
        if reason is None:
            if rule_range is not None and (rule_range.start, rule_range.end) != (start, end):
                _LOGGER.debug(
                    "intent.time_mismatch llm={llm_start}/{llm_end} rule={rule_start}/{rule_end}",
                    llm_start=start.isoformat(),
                    llm_end=end.isoformat(),
                    rule_start=rule_range.start.isoformat(),
                    rule_end=rule_range.end.isoformat(),
                )
            return _ModelRange(start=start, end=end)
        if reason == "too_long":
            return _ModelRange(oversize=(start, end))
        return _ModelRange(unusable=True)


def clarification_for(intent: CapabilityIntent) -> str | None:
    """Deterministic short clarification; no extra model call is required."""
    if not intent.needs_clarification:
        return None
    if intent.capability_id is None or "capability" in intent.ambiguous:
        return _CAPABILITY_CLARIFICATION
    for name in (*intent.missing, *intent.ambiguous):
        prompt = _CLARIFICATION_PROMPTS.get(name)
        if prompt is not None:
            return prompt
    return _CAPABILITY_CLARIFICATION


def _confidence(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return min(1.0, max(0.0, float(raw)))


def _string_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    items = cast("list[object]", raw)
    return tuple(item.strip() for item in items if isinstance(item, str) and item.strip())


def _is_chitchat(raw_type: object, raw_capability: object) -> bool:
    """Chit-chat when ``type`` says so; legacy payloads infer from the id."""
    if isinstance(raw_type, str):
        lowered = raw_type.strip().lower()
        if lowered in {"chitchat", "闲聊"}:
            return True
        if lowered in {"capability", "能力"}:
            return False
    return isinstance(raw_capability, str) and raw_capability.strip().lower() == "chitchat"


def _trimmed_text(raw: object) -> str | None:
    """Optional free-text field (``content`` / ``rewrite_query``), trimmed."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _code_list(raw: object) -> tuple[str, ...]:
    items = _string_list(raw)
    return tuple(dict.fromkeys(item[:_MAX_CODE_CHARS] for item in items))[:_MAX_LIST_ITEMS]


def _parse_datetime(raw: object, tz_name: str) -> datetime | None:
    """Parse a model-emitted ISO instant, localizing a naive value.

    The model routinely omits the offset; reading a naive value as the factory's
    local wall clock is the same convention the reviewed rule layer uses, so a
    bare ``2026-08-01`` denotes the same instants on both paths.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc)


def dump_intent(intent: CapabilityIntent) -> str:
    """Compact, non-sensitive projection used in phase events and tests."""
    return json.dumps(
        {
            "capability_id": intent.capability_id,
            "confidence": round(intent.confidence, 3),
            "missing": list(intent.missing),
            "ambiguous": list(intent.ambiguous),
            "filled": sorted(intent.slots.filled_names()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


__all__ = [
    "ALLOWED_SLOT_NAMES",
    "MIN_CAPABILITY_CONFIDENCE",
    "REJECTED_SLOT_NAMES",
    "SYSTEM_PROMPT",
    "WORKSHOP_OUTPUT_ROUTING_RULE",
    "CapabilityCatalog",
    "CapabilityIntentParser",
    "CapabilitySpec",
    "ClarificationLimitError",
    "IntentParseOutcome",
    "ParsedIntent",
    "build_intent_messages",
    "clarification_for",
    "dump_intent",
    "progress_flow_routing_block",
    "workshop_output_routing_block",
]
