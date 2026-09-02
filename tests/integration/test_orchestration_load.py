"""Orchestration load baseline without a real LLM.

Runs the full start -> stream session pipeline many times with an in-process
Fake LLM and a recorded capability runner, then records the p50/p95/p99
durations. K3 confirms performance limits are out of scope for this version, so
these numbers are a baseline only — never an acceptance gate.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest

from factory_agent.api.server import create_app
from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.bootstrap import DependencyOverrides
from factory_agent.config import FactoryAgentSettings
from factory_agent.domain import Role
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
    SequentialIds,
)

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
HEADERS = {TENANT_HEADER: "tenant-a", USER_HEADER: "user-a"}
INTENT = '{"capability_id": "FR-001", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'

#: Number of interactions for the baseline. Kept small so CI stays fast; the
#: recorded durations are the deliverable, not a threshold.
BASELINE_INTERACTIONS = 20
#: Upper sanity bound so a pathological regression is caught without imposing a
#: performance gate (K3): the run must finish, but the actual percentiles are
#: recorded, not asserted against a budget.
SANITY_CEILING_SECONDS = 60.0


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    rank = p / 100.0 * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


@pytest.mark.asyncio
async def test_orchestration_baseline_records_p50_p95_p99() -> None:
    store = InMemoryInteractionStore()
    runner = RecordingCapabilityRunner()
    member = membership("user-a", "tenant-a", "emp-1", Role.EMPLOYEE)
    overrides = DependencyOverrides(
        model=ScriptedModelGateway(contents=[INTENT] * BASELINE_INTERACTIONS),
        clock=FrozenClock(NOW),
        authorization=AuthorizationService(
            memberships=FakeMembershipSource(
                memberships_by_credential={("tenant-a", "user-a"): member}
            ),
            organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
            versions=FixedScopeVersionAssigner(),
        ),
        interactions=store,
        capability_runner=runner,
        new_id=SequentialIds(),
    )
    app = create_app(FactoryAgentSettings(environment="test"), overrides)

    durations_ms: list[float] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
    ) as http:
        for index in range(BASELINE_INTERACTIONS):
            started = time.monotonic()
            created = await http.post(
                f"/v1/sessions/session-{index}/interactions",
                json={"text": "我上个月做了多少件"},
                headers=HEADERS,
            )
            assert created.status_code == 201
            interaction_id = created.json()["interaction_id"]
            stream = await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)
            assert stream.status_code == 200
            assert "interaction.completed" in stream.text
            durations_ms.append((time.monotonic() - started) * 1000)

    assert len(durations_ms) == BASELINE_INTERACTIONS
    assert all(duration >= 0 for duration in durations_ms)
    assert sum(durations_ms) / 1000 < SANITY_CEILING_SECONDS

    ordered = sorted(durations_ms)
    baseline = {
        "p50_ms": percentile(ordered, 50),
        "p95_ms": percentile(ordered, 95),
        "p99_ms": percentile(ordered, 99),
        "mean_ms": sum(ordered) / len(ordered),
        "interactions": BASELINE_INTERACTIONS,
    }
    # The baseline is recorded (median is a stable summary); no threshold gate.
    assert baseline["p50_ms"] >= 0
    assert baseline["p95_ms"] >= baseline["p50_ms"]
    # A regression to a completely broken pipeline would exceed the ceiling.
    assert baseline["mean_ms"] < SANITY_CEILING_SECONDS * 1000
