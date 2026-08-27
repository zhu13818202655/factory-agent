"""Offline intent evaluation scorer (dev dependency, Story 8).

Loads ``tests/data/intent_eval_set.json`` and runs every case through the
deterministic ``CapabilityIntentParser.interpret`` — the same path the session
pipeline uses after a model returns a payload. The fixture pins interpretation,
not a model name, so the same set can score any candidate model behind a logical
alias: point ``parse`` at a live model and compare against ``expect``.

Run: ``uv run --no-sync python -m tests.eval.score_intent``
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from factory_agent.application.intent import (
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
    clarification_for,
)
from factory_agent.domain import CapabilityId
from tests.support.session import ScriptedModelGateway

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
DEFAULT_FIXTURE = DATA_ROOT / "intent_eval_set.json"


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    group: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Score:
    total: int
    passed: int
    failures: tuple[CaseResult, ...]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def load_fixture(path: Path | None = None) -> dict[str, Any]:
    active = path or DEFAULT_FIXTURE
    return cast(dict[str, Any], json.loads(active.read_text(encoding="utf-8")))


def build_parser(fixture: dict[str, Any], reference_now: datetime) -> CapabilityIntentParser:
    specs = tuple(
        CapabilitySpec(
            capability_id=CapabilityId(str(item["capability_id"])),
            title=str(item.get("title", "")),
            required_slots=tuple(str(slot) for slot in item.get("required_slots", ())),
        )
        for item in fixture["capabilities"]
    )
    return CapabilityIntentParser(
        ScriptedModelGateway(contents=[]),
        CapabilityCatalog(specs=specs),
        model_alias="factory-fast",
        timezone_name=str(fixture["timezone"]),
    )


def run_case(
    parser: CapabilityIntentParser,
    case: dict[str, Any],
    reference_now: datetime,
) -> CaseResult:
    case_id = str(case["id"])
    timezone_name = str(case.get("timezone", "Asia/Shanghai"))
    try:
        intent, rejected = parser.interpret(
            cast("dict[str, object]", case["model_payload"]), now=reference_now
        )
    except Exception as exc:  # noqa: BLE001 - scorer reports the failure
        return CaseResult(case_id, str(case.get("group", "")), False, f"raised: {exc!r}")

    expected = cast(dict[str, Any], case["expect"])
    actual_capability = str(intent.capability_id) if intent.capability_id is not None else None
    if actual_capability != expected.get("capability_id"):
        return CaseResult(
            case_id,
            str(case.get("group", "")),
            False,
            f"capability {actual_capability!r} != {expected.get('capability_id')!r}",
        )

    actual_range = _time_range(intent, timezone_name)
    if actual_range != expected.get("time_range"):
        return CaseResult(
            case_id,
            str(case.get("group", "")),
            False,
            f"time_range {actual_range!r} != {expected.get('time_range')!r}",
        )

    if list(intent.missing) != list(expected.get("missing", ())):
        return CaseResult(
            case_id,
            str(case.get("group", "")),
            False,
            f"missing {intent.missing!r} != {expected.get('missing')!r}",
        )
    if list(intent.ambiguous) != list(expected.get("ambiguous", ())):
        return CaseResult(
            case_id,
            str(case.get("group", "")),
            False,
            f"ambiguous {intent.ambiguous!r} != {expected.get('ambiguous')!r}",
        )
    if clarification_for(intent) != expected.get("clarification"):
        return CaseResult(
            case_id,
            str(case.get("group", "")),
            False,
            f"clarification {clarification_for(intent)!r} != {expected.get('clarification')!r}",
        )
    if "rejected_slots" in expected and set(rejected) != set(expected["rejected_slots"]):
        return CaseResult(
            case_id,
            str(case.get("group", "")),
            False,
            f"rejected_slots {set(rejected)!r} != {set(expected['rejected_slots'])!r}",
        )
    return CaseResult(case_id, str(case.get("group", "")), True, "")


def score(fixture: dict[str, Any] | None = None) -> Score:
    active = fixture or load_fixture()
    reference_now = datetime.fromisoformat(str(active["reference_now"]).replace("Z", "+00:00"))
    parser = build_parser(active, reference_now)
    results = [run_case(parser, case, reference_now) for case in active["cases"]]
    passed = sum(1 for result in results if result.passed)
    failures = tuple(result for result in results if not result.passed)
    return Score(total=len(results), passed=passed, failures=failures)


def _time_range(intent: Any, timezone_name: str) -> list[str] | None:
    start = intent.slots.time_range_start
    end = intent.slots.time_range_end
    if start is None or end is None:
        return None
    timezone = ZoneInfo(timezone_name)
    return [start.astimezone(timezone).isoformat(), end.astimezone(timezone).isoformat()]


def main() -> None:
    result = score()
    print(f"intent eval: {result.passed}/{result.total} passed ({result.pass_rate:.0%})")
    for failure in result.failures:
        print(f"  FAIL {failure.case_id}: {failure.detail}")


if __name__ == "__main__":
    main()
