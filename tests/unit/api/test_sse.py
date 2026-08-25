from __future__ import annotations

import json

import pytest

from factory_agent.api.sse import encode_event, parse_last_event_id, sanitize
from factory_agent.domain import (
    INTERACTION_STARTED,
    SessionEvent,
    terminal_event_name,
)
from factory_agent.domain.session import InteractionStatus


def test_frame_carries_the_durable_sequence_as_the_event_id() -> None:
    frame = encode_event(SessionEvent(sequence=12, name=INTERACTION_STARTED, data={"a": 1}))

    assert frame.startswith("id: 12\nevent: interaction.started\ndata: ")
    assert frame.endswith("\n\n")


def test_payload_is_utf8_json_without_escaping() -> None:
    frame = encode_event(SessionEvent(sequence=1, name="x", data={"q": "本月产量"}))

    assert "本月产量" in frame
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {"q": "本月产量"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_never_reach_the_wire(value: float) -> None:
    frame = encode_event(SessionEvent(sequence=1, name="x", data={"ratio": value}))

    assert "NaN" not in frame
    assert "Infinity" not in frame
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {"ratio": None}


def test_nested_non_finite_values_are_sanitized() -> None:
    assert sanitize({"a": [float("nan")]}) == {"a": [None]}
    assert sanitize({"a": {"b": float("inf")}}) == {"a": {"b": None}}


def test_finite_floats_are_preserved() -> None:
    assert sanitize({"a": 1.5}) == {"a": 1.5}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), ("", 0), ("abc", 0), ("-4", 0), ("7", 7), (" 9 ", 9)],
)
def test_last_event_id_parsing_is_defensive(raw: str | None, expected: int) -> None:
    assert parse_last_event_id(raw) == expected


def test_terminal_event_names_are_stable_wire_contracts() -> None:
    assert terminal_event_name(InteractionStatus.COMPLETED) == "interaction.completed"
    assert terminal_event_name(InteractionStatus.FAILED) == "interaction.failed"
    assert terminal_event_name(InteractionStatus.CANCELLED) == "interaction.cancelled"
