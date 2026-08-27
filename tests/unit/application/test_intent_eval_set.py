"""Fixed evaluation set for intent, slots, clarification, and grounding.

The set pins the deterministic interpretation of a model payload, so it scores
any candidate model behind the logical alias without hard-coding a model name or
quantization scheme. Model selection changes the payloads a real model produces;
it must never change the rules asserted here.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from factory_agent.application.intent import (
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
    clarification_for,
)
from factory_agent.domain import CapabilityId
from tests.support.session import ScriptedModelGateway

EVAL_SET_PATH = Path(__file__).resolve().parents[2] / "data" / "intent_eval_set.json"
EVAL_SET: dict[str, Any] = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = EVAL_SET["cases"]
NOW = datetime.fromisoformat(EVAL_SET["reference_now"])
TIMEZONE: str = EVAL_SET["timezone"]

CATALOG = CapabilityCatalog(
    specs=tuple(
        CapabilitySpec(
            capability_id=CapabilityId(entry["capability_id"]),
            title=entry["title"],
            required_slots=tuple(entry["required_slots"]),
        )
        for entry in EVAL_SET["capabilities"]
    )
)

#: The alias, never a concrete model, is what the evaluation runs against.
MODEL_ALIAS = "factory-fast"


def parser() -> CapabilityIntentParser:
    return CapabilityIntentParser(
        ScriptedModelGateway(), CATALOG, model_alias=MODEL_ALIAS, timezone_name=TIMEZONE
    )


def case_ids() -> list[str]:
    return [str(case["id"]) for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_evaluation_case_matches_the_expected_interpretation(case: dict[str, Any]) -> None:
    expected = cast("dict[str, Any]", case["expect"])
    intent, rejected = parser().interpret(case["model_payload"], now=NOW)

    assert intent.capability_id == expected["capability_id"]
    assert list(intent.missing) == expected["missing"]
    assert sorted(intent.ambiguous) == sorted(expected["ambiguous"])
    assert clarification_for(intent) == expected["clarification"]
    assert list(rejected) == expected.get("rejected_slots", [])

    if expected["time_range"] is None:
        assert intent.slots.time_range_start is None
        assert intent.slots.time_range_end is None
    else:
        start, end = expected["time_range"]
        assert intent.slots.time_range_start == datetime.fromisoformat(start)
        assert intent.slots.time_range_end == datetime.fromisoformat(end)

    if "order_codes" in expected:
        assert list(intent.slots.order_codes) == expected["order_codes"]


def test_every_evaluation_group_is_represented() -> None:
    groups = {str(case["group"]) for case in CASES}

    # Story 8 adds scope and fault-recovery to the original four groups.
    assert groups == {"intent", "slots", "clarification", "grounding", "scope", "fault-recovery"}


def test_grounding_cases_never_leak_a_rejected_slot_into_the_intent() -> None:
    for case in (entry for entry in CASES if entry["group"] == "grounding"):
        intent, rejected = parser().interpret(case["model_payload"], now=NOW)

        assert rejected, f"{case['id']} must record the stripped slots"
        serialized = json.dumps(asdict(intent.slots), default=str, ensure_ascii=False)
        for name in rejected:
            assert name not in serialized


def test_the_set_pins_no_model_name_or_quantization() -> None:
    text = EVAL_SET_PATH.read_text(encoding="utf-8").lower()

    for token in ("qwen", "deepseek", "gpt-", "llama", "awq", "gptq", "int4", "int8", "fp8"):
        assert token not in text
