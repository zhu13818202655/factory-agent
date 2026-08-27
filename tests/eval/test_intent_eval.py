"""Intent evaluation fixture is fully green and covers the required groups.

The fixed ``tests/data/intent_eval_set.json`` fixture pins the deterministic
interpretation of model payloads across intent, slots, clarification, scope,
grounding, and fault-recovery groups. A regression in the interpreter fails
here, independent of any model choice.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.eval.score_intent import load_fixture, score

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
FIXTURE = DATA_ROOT / "intent_eval_set.json"


def test_intent_eval_set_scores_fully_green() -> None:
    result = score(load_fixture(FIXTURE))

    assert result.total > 0
    assert result.failures == ()
    assert result.pass_rate == 1.0


def test_intent_eval_set_covers_all_required_groups() -> None:
    fixture = load_fixture(FIXTURE)
    groups = {case["group"] for case in fixture["cases"]}

    assert {"intent", "slots", "clarification", "scope", "grounding", "fault-recovery"} <= groups


def test_intent_eval_set_reference_is_frozen_and_parsable() -> None:
    fixture = load_fixture(FIXTURE)

    assert isinstance(fixture["reference_now"], str)
    assert fixture["timezone"] == "Asia/Shanghai"
    assert all("id" in case and "group" in case for case in fixture["cases"])


def test_fixture_is_valid_json_on_disk() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert len(parsed["cases"]) >= 19  # intent/slots/clarification/scope/grounding/fault-recovery
