from __future__ import annotations

from datetime import datetime, timezone

from factory_agent.application.context import (
    ConversationTurn,
    IntentPatch,
    compact_history,
    merge_intent,
    summarize_assistant_reply,
)
from factory_agent.domain import (
    CapabilityId,
    CapabilityIntent,
    IntentSlots,
    InteractionStatus,
)

JULY = datetime(2026, 7, 1, tzinfo=timezone.utc)
AUGUST = datetime(2026, 8, 1, tzinfo=timezone.utc)


def turn(
    user_text: str,
    assistant_text: str = "好的",
    status: InteractionStatus = InteractionStatus.COMPLETED,
    result_row_count: int | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        user_text=user_text,
        assistant_text=assistant_text,
        status=status,
        result_row_count=result_row_count,
    )


def test_empty_history_produces_no_messages() -> None:
    assert compact_history(()) == ()


def test_history_keeps_only_the_configured_turn_budget() -> None:
    turns = tuple(turn(f"问题{index}") for index in range(12))

    messages = compact_history(turns, max_turns=3)

    assert [message.content for message in messages if message.role == "user"] == [
        "问题9",
        "问题10",
        "问题11",
    ]


def test_result_bearing_reply_never_carries_detail_rows() -> None:
    summary = summarize_assistant_reply(
        ConversationTurn(
            user_text="上月工资",
            assistant_text="张三 8200.00 元；李四 7600.00 元",
            status=InteractionStatus.COMPLETED,
            capability_id=CapabilityId("FR-008"),
            result_row_count=2,
        )
    )

    assert "8200" not in summary
    assert "张三" not in summary
    assert "FR-008" in summary


def test_failed_and_cancelled_replies_collapse_to_a_neutral_summary() -> None:
    for status in (InteractionStatus.FAILED, InteractionStatus.CANCELLED):
        summary = summarize_assistant_reply(
            turn("上月工资", "内部错误：连接 10.0.0.4 失败", status=status)
        )

        assert "10.0.0.4" not in summary
        assert summary == "上一轮请求未完成。"


def test_single_oversized_turn_is_truncated_rather_than_dropped() -> None:
    messages = compact_history((turn("问" * 500, "答" * 500),), max_chars=100)

    assert sum(len(message.content) for message in messages) <= 100
    assert messages[0].role == "user"


def test_blank_user_text_is_skipped() -> None:
    assert compact_history((turn("   "),)) == ()


def base_intent() -> CapabilityIntent:
    return CapabilityIntent(
        capability_id=CapabilityId("FR-001"),
        confidence=0.9,
        slots=IntentSlots(
            time_range_start=JULY,
            time_range_end=AUGUST,
            time_expression="上个月",
            dept_names=("一车间",),
        ),
        missing=("order_codes",),
        ambiguous=("time_range",),
    )


def test_explicit_follow_up_period_replaces_the_previous_period() -> None:
    september = datetime(2026, 9, 1, tzinfo=timezone.utc)

    merged = merge_intent(
        base_intent(),
        IntentPatch(time_range_start=AUGUST, time_range_end=september, time_expression="本月"),
    )

    assert merged.slots.time_range_start == AUGUST
    assert merged.slots.time_range_end == september
    assert merged.slots.time_expression == "本月"


def test_empty_patch_preserves_every_established_value() -> None:
    merged = merge_intent(base_intent(), IntentPatch())

    assert merged.capability_id == CapabilityId("FR-001")
    assert merged.slots.dept_names == ("一车间",)
    assert merged.slots.time_range_start == JULY


def test_empty_lists_and_null_fields_do_not_erase_values() -> None:
    merged = merge_intent(
        base_intent(), IntentPatch(dept_names=(), order_codes=(), time_expression=None)
    )

    assert merged.slots.dept_names == ("一车间",)
    assert merged.slots.time_expression == "上个月"


def test_merge_resets_stale_missing_and_ambiguous_diagnostics() -> None:
    merged = merge_intent(base_intent(), IntentPatch(dept_names=("二车间",)))

    assert merged.slots.dept_names == ("二车间",)
    assert merged.missing == ()
    assert merged.ambiguous == ()


def test_patch_can_switch_capability_for_re_authorization() -> None:
    merged = merge_intent(base_intent(), IntentPatch(capability_id=CapabilityId("FR-010")))

    assert merged.capability_id == CapabilityId("FR-010")


def test_is_empty_reports_a_no_op_patch() -> None:
    assert IntentPatch().is_empty() is True
    assert IntentPatch(dept_names=("二车间",)).is_empty() is False
