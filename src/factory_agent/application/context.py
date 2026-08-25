"""Bounded conversation context and follow-up patch merging.

History carries no previous detail rows, no previous authorization scope, and no
sensitive raw values. A follow-up patch only overwrites fields the user actually
restated: unsupplied fields and empty collections never erase an established
value, and merging resets the stale ``missing``/``ambiguous`` diagnostics so the
completeness check can repopulate them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from factory_agent.domain import (
    CapabilityId,
    CapabilityIntent,
    IntentSlots,
    InteractionStatus,
)
from factory_agent.ports import ModelMessage

DEFAULT_MAX_TURNS = 8
DEFAULT_MAX_CHARS = 8192

_UNFINISHED_SUMMARY = "上一轮请求未完成。"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One completed turn, already stripped of detail rows and scope IDs."""

    user_text: str
    assistant_text: str
    status: InteractionStatus
    capability_id: CapabilityId | None = None
    result_row_count: int | None = None


def summarize_assistant_reply(turn: ConversationTurn) -> str:
    """Replace a result-bearing reply with a row-count summary, never its rows."""
    if turn.status in (InteractionStatus.FAILED, InteractionStatus.CANCELLED):
        return _UNFINISHED_SUMMARY
    if turn.result_row_count is not None:
        capability = turn.capability_id or "查询"
        return f"返回了 {capability} 的结果表（{turn.result_row_count} 行）。"
    return turn.assistant_text


def compact_history(
    turns: tuple[ConversationTurn, ...],
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[ModelMessage, ...]:
    """Compact recent turns into a bounded chat history for the model gateway."""
    if not turns:
        return ()

    pairs: list[tuple[ModelMessage, ModelMessage]] = []
    for turn in turns[-max_turns:]:
        user_text = turn.user_text.strip()
        if not user_text:
            continue
        pairs.append(
            (
                ModelMessage(role="user", content=user_text),
                ModelMessage(role="assistant", content=summarize_assistant_reply(turn)),
            )
        )

    while pairs and _pair_chars(pairs) > max_chars:
        if len(pairs) == 1:
            return _truncate_pair(pairs[0], max_chars)
        pairs.pop(0)

    return tuple(message for pair in pairs for message in pair)


def _pair_chars(pairs: list[tuple[ModelMessage, ModelMessage]]) -> int:
    return sum(len(message.content) for pair in pairs for message in pair)


def _truncate_pair(
    pair: tuple[ModelMessage, ModelMessage], max_chars: int
) -> tuple[ModelMessage, ...]:
    user, assistant = pair
    user_budget = max(1, max_chars // 2)
    truncated_user = ModelMessage(role="user", content=user.content[:user_budget])
    assistant_budget = max(0, max_chars - len(truncated_user.content))
    truncated_assistant = ModelMessage(
        role="assistant", content=assistant.content[:assistant_budget]
    )
    return (truncated_user, truncated_assistant)


@dataclass(frozen=True, slots=True)
class IntentPatch:
    """Fields a follow-up turn explicitly restated.

    ``None`` and empty collections both mean "not supplied" and never clear an
    established value.
    """

    capability_id: CapabilityId | None = None
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    time_expression: str | None = None
    order_codes: tuple[str, ...] = ()
    plan_codes: tuple[str, ...] = ()
    style_codes: tuple[str, ...] = ()
    dept_names: tuple[str, ...] = ()
    employee_names: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not any(
            (
                self.capability_id,
                self.time_range_start,
                self.time_range_end,
                self.time_expression,
                self.order_codes,
                self.plan_codes,
                self.style_codes,
                self.dept_names,
                self.employee_names,
            )
        )


def merge_intent(base: CapabilityIntent, patch: IntentPatch) -> CapabilityIntent:
    """Apply a follow-up patch; the caller must re-authorize the merged intent."""
    slots = _merge_slots(base.slots, patch)
    return CapabilityIntent(
        capability_id=patch.capability_id or base.capability_id,
        confidence=base.confidence,
        slots=slots,
        missing=(),
        ambiguous=(),
    )


def _merge_slots(base: IntentSlots, patch: IntentPatch) -> IntentSlots:
    merged = base
    if patch.time_range_start is not None and patch.time_range_end is not None:
        merged = replace(
            merged,
            time_range_start=patch.time_range_start,
            time_range_end=patch.time_range_end,
            time_expression=patch.time_expression or base.time_expression,
        )
    elif patch.time_expression:
        merged = replace(merged, time_expression=patch.time_expression)

    for name in ("order_codes", "plan_codes", "style_codes", "dept_names", "employee_names"):
        value: tuple[str, ...] = getattr(patch, name)
        if value:
            merged = replace(merged, **{name: value})
    return merged


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_TURNS",
    "ConversationTurn",
    "IntentPatch",
    "compact_history",
    "merge_intent",
    "summarize_assistant_reply",
]
