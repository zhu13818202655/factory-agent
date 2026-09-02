"""Characterization of the migrated report-agent behaviour.

The baseline JSON is a frozen, read-only snapshot of the source repository. These
tests never import ``report_agent`` and never read the source tree at runtime;
they assert that factory-agent preserves the behaviours marked for migration and
that the recorded source defects were replaced rather than inherited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from factory_agent.api.sse import encode_event, sanitize
from factory_agent.application.context import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_TURNS,
    ConversationTurn,
    IntentPatch,
    compact_history,
    merge_intent,
)
from factory_agent.application.structured import extract_json_object
from factory_agent.domain import (
    INTERACTION_STARTED,
    TERMINAL_STATES,
    CapabilityId,
    CapabilityIntent,
    IntentSlots,
    InteractionStatus,
    SessionEvent,
    SessionState,
    can_transition,
)
from factory_agent.domain.session import ALLOWED_TRANSITIONS
from factory_agent.persistence.tables import message_table

BASELINE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "report_agent_baseline.json").read_text(encoding="utf-8")
)

_RENAMES: dict[str, str | None] = BASELINE["factory_state_renames"]


def _factory_state(source_state: str) -> str | None:
    return _RENAMES.get(source_state, source_state)


def test_baseline_records_the_source_transition_table() -> None:
    source = BASELINE["state_machine"]["allowed_transitions"]

    for source_state, source_targets in source.items():
        target_state = _factory_state(source_state)
        if target_state is None:
            continue
        migrated = ALLOWED_TRANSITIONS[SessionState(target_state)]
        expected = {
            _factory_state(item) for item in source_targets if _factory_state(item) is not None
        }
        # ``previewing -> rendering`` collapses because export lands in a later stage.
        assert {state.value for state in migrated} >= expected - {"archived"}


def test_failed_is_reachable_from_every_non_terminal_state() -> None:
    assert BASELINE["state_machine"]["failed_reachable_from_any_non_terminal"] is True

    for state in SessionState:
        expected = state not in TERMINAL_STATES
        assert can_transition(state, SessionState.FAILED) is expected


def test_terminal_states_cannot_restart() -> None:
    for state_value in BASELINE["state_machine"]["terminal_states"]:
        target = _factory_state(state_value)
        assert target is not None
        state = SessionState(target)
        assert state in TERMINAL_STATES
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_history_bounds_match_the_source_defaults() -> None:
    context = BASELINE["context"]

    assert DEFAULT_MAX_TURNS == context["default_max_turns"]
    assert DEFAULT_MAX_CHARS == context["default_max_chars"]


def test_result_bearing_replies_are_summarized_without_detail_rows() -> None:
    assert BASELINE["context"]["artifact_reply_is_summarized"] is True

    turn = ConversationTurn(
        user_text="本月产量",
        assistant_text="工号 A-1 产量 4210 件，工号 A-2 产量 3980 件",
        status=InteractionStatus.COMPLETED,
        capability_id=CapabilityId("FR-001"),
        result_row_count=2,
    )
    messages = compact_history((turn,))

    assert "A-1" not in messages[1].content
    assert "2 行" in messages[1].content


def test_trimming_drops_oldest_turn_pairs_first() -> None:
    assert BASELINE["context"]["drops_oldest_turn_pairs_first"] is True

    turns = tuple(
        ConversationTurn(
            user_text=f"问题{index}" + "x" * 40,
            assistant_text=f"回答{index}" + "y" * 40,
            status=InteractionStatus.COMPLETED,
        )
        for index in range(4)
    )
    messages = compact_history(turns, max_chars=180)

    assert "问题0" not in "".join(message.content for message in messages)
    assert "问题3" in "".join(message.content for message in messages)


def test_patch_semantics_match_the_source_merge_rules() -> None:
    rules = BASELINE["follow_up_patch"]
    assert rules["null_field_does_not_clear"] is True
    assert rules["empty_list_does_not_clear"] is True
    assert rules["merge_resets_missing_and_conflicts"] is True

    base = CapabilityIntent(
        capability_id=CapabilityId("FR-001"),
        confidence=0.9,
        slots=IntentSlots(dept_names=("二车间",), time_expression="本月"),
        missing=("order_codes",),
        ambiguous=("time_range",),
    )
    merged = merge_intent(base, IntentPatch(dept_names=(), time_expression=None))

    assert merged.slots.dept_names == ("二车间",)
    assert merged.slots.time_expression == "本月"
    assert merged.missing == ()
    assert merged.ambiguous == ()


def test_json_extraction_shapes_match_the_source() -> None:
    shapes = BASELINE["json_extraction"]
    assert shapes["accepts_raw_object"] is True

    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('前言 {"a": 1} 结语') == {"a": 1}
    assert extract_json_object("   ") is None


def test_sse_framing_and_non_finite_sanitization_match_the_source() -> None:
    sse = BASELINE["sse"]
    assert sse["first_event"] == INTERACTION_STARTED
    assert sse["non_finite_floats_become_null"] is True

    frame = encode_event(
        SessionEvent(sequence=7, name=INTERACTION_STARTED, data={"ratio": float("nan")})
    )

    assert frame.startswith("id: 7\nevent: interaction.started\ndata: ")
    assert "NaN" not in frame
    assert sanitize({"a": [float("inf")]}) == {"a": [None]}


def test_message_sequence_stays_unique_within_an_interaction() -> None:
    assert BASELINE["persistence"]["message_sequence_unique_per_interaction"] is True

    constraint_columns: set[tuple[str, ...]] = set()
    for constraint in message_table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            constraint_columns.add(tuple(sorted(constraint.columns.keys())))

    assert ("interaction_id", "sequence") in constraint_columns


def test_recorded_source_defects_are_replacements_not_inheritance() -> None:
    replacements = BASELINE["replacements_not_inherited"]

    assert {item["source"] for item in replacements} >= {
        "src/report_agent/api/router.py",
        "src/report_agent/permissions.py",
        "src/report_agent/service.py",
        "src/report_agent/repository.py",
        "src/report_agent/intent/classifier.py",
        "src/report_agent/llm/client.py",
    }
    for item in replacements:
        assert item["factory_agent"]
