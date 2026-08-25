from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.domain import (
    TERMINAL_STATES,
    InteractionStatus,
    InvalidStateTransitionError,
    SessionState,
    SessionStateMachine,
    assert_transition,
    can_transition,
    is_terminal,
    terminal_event_name,
)
from factory_agent.domain.session import ALLOWED_TRANSITIONS

INSTANT = datetime(2026, 8, 24, tzinfo=timezone.utc)

LEGAL_TRANSITIONS = [
    (source, target) for source, targets in ALLOWED_TRANSITIONS.items() for target in targets
]

ILLEGAL_TRANSITIONS = [
    (source, target)
    for source in SessionState
    for target in SessionState
    if target is not SessionState.FAILED and target not in ALLOWED_TRANSITIONS[source]
]


@pytest.mark.parametrize(("source", "target"), LEGAL_TRANSITIONS)
def test_every_declared_edge_is_legal(source: SessionState, target: SessionState) -> None:
    assert can_transition(source, target) is True


@pytest.mark.parametrize(("source", "target"), ILLEGAL_TRANSITIONS)
def test_every_undeclared_edge_is_rejected(source: SessionState, target: SessionState) -> None:
    assert can_transition(source, target) is False
    with pytest.raises(InvalidStateTransitionError):
        assert_transition(source, target)


@pytest.mark.parametrize("state", list(SessionState))
def test_failed_is_reachable_from_every_non_terminal_state(state: SessionState) -> None:
    assert can_transition(state, SessionState.FAILED) is (state not in TERMINAL_STATES)


TERMINAL_STATE_CASES: list[SessionState] = sorted(TERMINAL_STATES, key=lambda item: item.name)


@pytest.mark.parametrize("state", TERMINAL_STATE_CASES)
def test_terminal_states_never_restart(state: SessionState) -> None:
    assert is_terminal(state) is True
    for target in SessionState:
        assert can_transition(state, target) is False


def test_transition_history_records_from_to_and_reason() -> None:
    machine = SessionStateMachine()

    machine = machine.transition_to(SessionState.AUTHORIZING, "intent_complete", INSTANT)
    machine = machine.transition_to(SessionState.EXECUTING, "authorized", INSTANT)

    assert machine.state is SessionState.EXECUTING
    assert [(entry.from_state, entry.to_state, entry.reason) for entry in machine.history] == [
        (SessionState.PARSING, SessionState.AUTHORIZING, "intent_complete"),
        (SessionState.AUTHORIZING, SessionState.EXECUTING, "authorized"),
    ]


def test_transition_does_not_mutate_the_previous_machine() -> None:
    machine = SessionStateMachine()

    advanced = machine.transition_to(SessionState.CLARIFYING, "slots_missing", INSTANT)

    assert machine.state is SessionState.PARSING
    assert machine.history == ()
    assert advanced.state is SessionState.CLARIFYING


def test_answered_allows_a_follow_up_turn_back_to_parsing() -> None:
    assert can_transition(SessionState.ANSWERED, SessionState.PARSING) is True


@pytest.mark.parametrize(
    ("status", "name"),
    [
        (InteractionStatus.COMPLETED, "interaction.completed"),
        (InteractionStatus.FAILED, "interaction.failed"),
        (InteractionStatus.CANCELLED, "interaction.cancelled"),
    ],
)
def test_terminal_event_names_are_one_per_status(status: InteractionStatus, name: str) -> None:
    assert terminal_event_name(status) == name


@pytest.mark.parametrize("status", [InteractionStatus.PENDING, InteractionStatus.RUNNING])
def test_non_terminal_statuses_have_no_terminal_event(status: InteractionStatus) -> None:
    with pytest.raises(ValueError, match="not a terminal"):
        terminal_event_name(status)
