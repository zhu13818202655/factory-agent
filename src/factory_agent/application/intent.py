"""Capability selection, slot extraction, and bounded clarification.

The parser replaces the report-agent free-form ``DraftFilterSpec`` with a typed
``CapabilityIntent`` restricted to registered capabilities. It never accepts
employee or department identifiers from the user or the model: those come only
from the trusted ``DataScope``. A low-confidence or incomplete result produces a
short clarification question instead of an unbounded query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from factory_agent.application.context import ConversationTurn, compact_history
from factory_agent.application.structured import (
    StructuredOutputError,
    request_structured_object,
)
from factory_agent.application.time_expressions import (
    TimeExpressionError,
    resolve_time_expression,
)
from factory_agent.domain import (
    CapabilityId,
    CapabilityIntent,
    IntentSlots,
)
from factory_agent.ports import ModelGateway, ModelMessage, ModelRequest, ModelStage

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
        "dept_names",
        "employee_names",
    }
)

#: Slot names that must never reach the executor even if a model emits them.
REJECTED_SLOT_NAMES: frozenset[str] = frozenset(
    {"employee_ids", "dept_ids", "tenant_id", "user_id", "scope", "sql", "url"}
)

_CLARIFICATION_PROMPTS: dict[str, str] = {
    "time_range": "请补充时间范围，例如“本月”“上周”或“2026-08”。",
    "order_codes": "请提供具体的订单号。",
    "plan_codes": "请提供具体的计划单号。",
    "style_codes": "请提供具体的款号。",
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
    required_slots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityCatalog:
    """Registered capabilities the parser is allowed to select."""

    specs: tuple[CapabilitySpec, ...] = ()

    def get(self, capability_id: str) -> CapabilitySpec | None:
        for spec in self.specs:
            if spec.capability_id == capability_id:
                return spec
        return None

    def describe(self) -> str:
        return "\n".join(
            f"- {spec.capability_id}: {spec.title} (required: "
            f"{', '.join(spec.required_slots) or 'none'})"
            for spec in self.specs
        )


SYSTEM_PROMPT = (
    "你是工厂业务助手的能力选择器。只能从给定的能力列表中选择一个 capability_id，"
    "不得发明新的能力，不得输出 SQL、URL、员工编号或部门编号。\n"
    "严格输出一个 JSON 对象：\n"
    '{"capability_id": "<列表中的 id 或 null>", "confidence": 0.0-1.0, '
    '"slots": {"time_expression": "...", "order_codes": [], "plan_codes": [], '
    '"style_codes": [], "dept_names": [], "employee_names": []}, "ambiguous": []}\n'
    "无法判断时把 capability_id 设为 null。不要输出解释或代码块。"
)


def build_intent_messages(
    user_text: str,
    catalog: CapabilityCatalog,
    history: tuple[ConversationTurn, ...] = (),
    *,
    max_turns: int,
    max_chars: int,
) -> tuple[ModelMessage, ...]:
    system = ModelMessage(
        role="system", content=f"{SYSTEM_PROMPT}\n\n可用能力:\n{catalog.describe()}"
    )
    compacted = compact_history(history, max_turns=max_turns, max_chars=max_chars)
    return (system, *compacted, ModelMessage(role="user", content=user_text))


@dataclass(frozen=True, slots=True)
class IntentParseOutcome:
    intent: CapabilityIntent
    clarification: str | None
    attempts: int
    actual_model: str
    duration_ms: int
    rejected_slots: tuple[str, ...] = ()


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
        max_history_chars: int = 8192,
        min_confidence: float = MIN_CAPABILITY_CONFIDENCE,
    ) -> None:
        self._gateway = gateway
        self._catalog = catalog
        self._model_alias = model_alias
        self._timezone_name = timezone_name
        self._max_repair_attempts = max_repair_attempts
        self._max_history_turns = max_history_turns
        self._max_history_chars = max_history_chars
        self._min_confidence = min_confidence

    async def parse(
        self,
        user_text: str,
        *,
        now: datetime,
        logical_call_id: str,
        history: tuple[ConversationTurn, ...] = (),
    ) -> IntentParseOutcome:
        if not user_text.strip():
            raise StructuredOutputError("user text is empty", attempts=0)

        request = ModelRequest(
            model_alias=self._model_alias,
            messages=build_intent_messages(
                user_text,
                self._catalog,
                history,
                max_turns=self._max_history_turns,
                max_chars=self._max_history_chars,
            ),
            stage=ModelStage.EXTRACT,
            logical_call_id=logical_call_id,
            json_output=True,
        )
        result = await request_structured_object(
            self._gateway, request, max_repair_attempts=self._max_repair_attempts
        )
        intent, rejected = self.interpret(result.payload, now=now)
        return IntentParseOutcome(
            intent=intent,
            clarification=clarification_for(intent),
            attempts=result.attempts,
            actual_model=result.response.actual_model,
            duration_ms=result.response.duration_ms,
            rejected_slots=rejected,
        )

    def interpret(
        self, payload: dict[str, object], *, now: datetime
    ) -> tuple[CapabilityIntent, tuple[str, ...]]:
        """Validate a raw model payload into a typed intent."""
        ambiguous = list(_string_list(payload.get("ambiguous")))
        confidence = _confidence(payload.get("confidence"))
        raw_slots = payload.get("slots")
        slots_mapping: dict[str, Any] = (
            cast("dict[str, Any]", raw_slots) if isinstance(raw_slots, dict) else {}
        )
        rejected: tuple[str, ...] = tuple(
            sorted(name for name in slots_mapping if name in REJECTED_SLOT_NAMES)
        )

        spec = self._resolve_capability(payload.get("capability_id"))
        if spec is None:
            ambiguous.append("capability")
            return (
                CapabilityIntent(
                    capability_id=None,
                    confidence=confidence,
                    ambiguous=tuple(dict.fromkeys(ambiguous)),
                ),
                rejected,
            )
        if confidence < self._min_confidence:
            ambiguous.append("capability")

        slots, slot_ambiguity = self._build_slots(slots_mapping, now=now)
        ambiguous.extend(slot_ambiguity)
        missing = tuple(name for name in spec.required_slots if name not in slots.filled_names())
        return (
            CapabilityIntent(
                capability_id=spec.capability_id,
                confidence=confidence,
                slots=slots,
                missing=missing,
                ambiguous=tuple(dict.fromkeys(ambiguous)),
            ),
            rejected,
        )

    def _resolve_capability(self, raw: object) -> CapabilitySpec | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        return self._catalog.get(raw.strip())

    def _build_slots(
        self, raw: dict[str, Any], *, now: datetime
    ) -> tuple[IntentSlots, tuple[str, ...]]:
        ambiguous: list[str] = []
        expression = raw.get("time_expression")
        start = _parse_datetime(raw.get("time_range_start"))
        end = _parse_datetime(raw.get("time_range_end"))

        if (start is None or end is None) and isinstance(expression, str) and expression.strip():
            try:
                resolved = resolve_time_expression(expression, now, self._timezone_name)
            except TimeExpressionError:
                ambiguous.append("time_range")
            else:
                start, end = resolved.start, resolved.end
        if start is not None and end is not None and start >= end:
            start, end = None, None
            ambiguous.append("time_range")

        return (
            IntentSlots(
                time_range_start=start,
                time_range_end=end,
                time_expression=expression.strip() if isinstance(expression, str) else None,
                order_codes=_code_list(raw.get("order_codes")),
                plan_codes=_code_list(raw.get("plan_codes")),
                style_codes=_code_list(raw.get("style_codes")),
                dept_names=_code_list(raw.get("dept_names")),
                employee_names=_code_list(raw.get("employee_names")),
            ),
            tuple(ambiguous),
        )


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


def _code_list(raw: object) -> tuple[str, ...]:
    items = _string_list(raw)
    return tuple(dict.fromkeys(item[:_MAX_CODE_CHARS] for item in items))[:_MAX_LIST_ITEMS]


def _parse_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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
    "CapabilityCatalog",
    "CapabilityIntentParser",
    "CapabilitySpec",
    "ClarificationLimitError",
    "IntentParseOutcome",
    "build_intent_messages",
    "clarification_for",
    "dump_intent",
]
