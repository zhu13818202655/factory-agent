from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from factory_agent.llm.health import EndpointHealthMonitor
from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment

CANARY_KEY = "sk-canary"


@dataclass
class ScriptedProbe:
    """A fake probe whose verdict depends on the endpoint and the call count.

    Maps ``api_base`` to a queue of verdicts; each call pops the head. Anything
    not scripted aborts the test loudly, so a probe loop that touches an
    unexpected endpoint fails here rather than silently passing.
    """

    scripts: dict[str, list[bool]] = field(default_factory=lambda: dict[str, list[bool]]())
    targets: list[ResolvedDeployment] = field(default_factory=lambda: list[ResolvedDeployment]())

    async def is_healthy(self, deployment: ResolvedDeployment) -> bool:
        self.targets.append(deployment)
        queue = self.scripts.get(deployment.api_base)
        if queue is None or not queue:
            raise AssertionError(f"no scripted verdict for endpoint {deployment.api_base!r}")
        return queue.pop(0)


@dataclass
class RaisingProbe:
    async def is_healthy(self, deployment: ResolvedDeployment) -> bool:
        raise RuntimeError("probe exploded")


def _qwen(base: str, alias: str = "factory-fast", priority: int = 1) -> ResolvedDeployment:
    return ResolvedDeployment(
        alias=alias,
        model="Qwen/Qwen3.8-27B-FP8",
        api_base=f"{base}/v1",
        api_key=CANARY_KEY,
        priority=priority,
        provider="openai",
    )


def _deepseek(alias: str = "factory-fast", priority: int = 2) -> ResolvedDeployment:
    return ResolvedDeployment(
        alias=alias,
        model="deepseek/deepseek-chat",
        api_base="https://api.deepseek.com/v1",
        api_key=CANARY_KEY,
        priority=priority,
    )


def _registry_for(endpoint: str) -> ModelRegistry:
    """An alias whose prio-1 endpoint is ``endpoint`` (order matters here)."""
    return ModelRegistry(
        version=1,
        deployments=(_qwen(endpoint), _deepseek()),
        fallbacks={"factory-fast": ()},
    )


def _applied() -> tuple[list[ModelRegistry], Callable[[ModelRegistry], None]]:
    log: list[ModelRegistry] = []

    def apply(registry: ModelRegistry) -> None:
        log.append(registry)

    return log, apply


def _alias_priorities(registry: ModelRegistry, alias: str) -> list[tuple[str, int]]:
    return [(d.api_base, d.priority) for d in registry.deployments if d.alias == alias]


@pytest.mark.asyncio
async def test_a_healthy_endpoint_keeps_the_reviewed_order() -> None:
    baseline = _registry_for("https://qwen.example")
    probe = ScriptedProbe(scripts={"https://qwen.example/v1": [True]})
    applied_log, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, probe, apply=apply)
    await monitor.refresh()

    assert _alias_priorities(monitor.registry, "factory-fast") == [
        ("https://qwen.example/v1", 1),
        ("https://api.deepseek.com/v1", 2),
    ]
    assert monitor.demoted_endpoints == frozenset()
    assert applied_log == [baseline]


@pytest.mark.asyncio
async def test_a_single_failure_does_not_demote() -> None:
    baseline = _registry_for("https://qwen.example")
    probe = ScriptedProbe(scripts={"https://qwen.example/v1": [False]})
    _, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, probe, failures_to_demote=2, apply=apply)
    await monitor.refresh()

    assert monitor.demoted_endpoints == frozenset()
    assert [d.priority for d in monitor.registry.deployments] == [1, 2]


@pytest.mark.asyncio
async def test_repeated_failures_demote_and_promote_the_backup() -> None:
    baseline = _registry_for("https://qwen.example")
    probe = ScriptedProbe(
        scripts={
            "https://qwen.example/v1": [False, False],
            "https://api.deepseek.com/v1": [True, True],
        }
    )
    applied_log, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, probe, failures_to_demote=2, apply=apply)
    await monitor.refresh()  # 1/2 failures on Qwen, not yet demoted
    await monitor.refresh()  # 2/2 failures → demoted

    assert monitor.demoted_endpoints == frozenset({"https://qwen.example/v1"})
    assert _alias_priorities(monitor.registry, "factory-fast") == [
        ("https://api.deepseek.com/v1", 1),
        ("https://qwen.example/v1", 2),
    ]
    # The published registry is the demoted copy, the baseline itself is not.
    assert applied_log and applied_log[-1] is not baseline
    assert baseline.deployments[0].priority == 1


@pytest.mark.asyncio
async def test_a_recovered_endpoint_restores_the_reviewed_order() -> None:
    baseline = _registry_for("https://qwen.example")
    probe = ScriptedProbe(
        scripts={
            "https://qwen.example/v1": [False, False, True],
            "https://api.deepseek.com/v1": [True, True, True],
        }
    )
    _, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, probe, failures_to_demote=2, apply=apply)
    await monitor.refresh()
    await monitor.refresh()
    assert monitor.demoted_endpoints == frozenset({"https://qwen.example/v1"})

    await monitor.refresh()

    assert monitor.demoted_endpoints == frozenset()
    assert _alias_priorities(monitor.registry, "factory-fast") == [
        ("https://qwen.example/v1", 1),
        ("https://api.deepseek.com/v1", 2),
    ]


@pytest.mark.asyncio
async def test_each_alias_is_demoted_independently() -> None:
    qwen = "https://qwen-fast.example"
    baseline = ModelRegistry(
        version=1,
        deployments=(
            _qwen(qwen, alias="factory-fast", priority=1),
            _deepseek(alias="factory-fast", priority=2),
            _qwen(qwen, alias="factory-summary", priority=1),
            _deepseek(alias="factory-summary", priority=2),
        ),
        fallbacks={"factory-fast": (), "factory-summary": ()},
    )
    probe = ScriptedProbe(
        scripts={
            f"{qwen}/v1": [False, False],
            "https://api.deepseek.com/v1": [True, True],
        }
    )
    _, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, probe, failures_to_demote=2, apply=apply)
    await monitor.refresh()
    await monitor.refresh()

    for alias in ("factory-fast", "factory-summary"):
        members = _alias_priorities(monitor.registry, alias)
        assert members == [
            ("https://api.deepseek.com/v1", 1),
            (f"{qwen}/v1", 2),
        ], alias


@pytest.mark.asyncio
async def test_probing_is_deduplicated_per_endpoint() -> None:
    qwen = "https://qwen-shared.example"
    baseline = ModelRegistry(
        version=1,
        deployments=(
            _qwen(qwen, alias="factory-fast", priority=1),
            _deepseek(alias="factory-fast", priority=2),
            _qwen(qwen, alias="factory-reasoning", priority=1),
            _deepseek(alias="factory-reasoning", priority=2),
            _qwen(qwen, alias="factory-summary", priority=1),
            _deepseek(alias="factory-summary", priority=2),
        ),
        fallbacks={"factory-fast": (), "factory-reasoning": (), "factory-summary": ()},
    )
    probe = ScriptedProbe(
        scripts={
            f"{qwen}/v1": [True],
            "https://api.deepseek.com/v1": [True],
        }
    )

    monitor = EndpointHealthMonitor(baseline, probe, apply=lambda _: None)
    await monitor.refresh()

    assert len(probe.targets) == 2
    assert {target.api_base for target in probe.targets} == {
        f"{qwen}/v1",
        "https://api.deepseek.com/v1",
    }


@pytest.mark.asyncio
async def test_a_raising_probe_counts_as_unhealthy() -> None:
    baseline = _registry_for("https://qwen.example")
    _, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, RaisingProbe(), failures_to_demote=1, apply=apply)
    await monitor.refresh()

    # Both endpoints got a raising probe; both are demoted.
    assert monitor.demoted_endpoints == frozenset(
        {"https://qwen.example/v1", "https://api.deepseek.com/v1"}
    )
    # Everything demoted means the reviewed order is already optimal.
    assert [d.priority for d in monitor.registry.deployments] == [1, 2]


@pytest.mark.asyncio
async def test_baseline_deployments_stay_immutable_under_demotion() -> None:
    baseline = _registry_for("https://qwen.example")
    snapshot = baseline.deployments
    probe = ScriptedProbe(
        scripts={
            "https://qwen.example/v1": [False, False],
            "https://api.deepseek.com/v1": [True, True],
        }
    )

    monitor = EndpointHealthMonitor(baseline, probe, failures_to_demote=2, apply=lambda _: None)
    await monitor.refresh()
    await monitor.refresh()

    assert baseline.deployments == snapshot
    assert snapshot[0].priority == 1


def test_with_demoted_endpoints_returns_self_when_unchanged() -> None:
    baseline = _registry_for("https://qwen.example")
    # Nothing demoted: the verdict cannot change anything.
    assert baseline.with_demoted_endpoints(set()) is baseline


def test_probe_targets_returns_one_representative_per_endpoint() -> None:
    qwen = "https://qwen.example"
    registry = ModelRegistry(
        version=1,
        deployments=(
            _qwen(qwen, alias="a", priority=1),
            _deepseek(alias="a", priority=2),
            _qwen(qwen, alias="b", priority=1),
            _deepseek(alias="b", priority=2),
        ),
        fallbacks={"a": (), "b": ()},
    )

    assert [d.api_base for d in registry.probe_targets()] == [
        f"{qwen}/v1",
        "https://api.deepseek.com/v1",
    ]


@pytest.mark.asyncio
async def test_apply_runs_on_every_refresh_so_a_late_recovery_propagates() -> None:
    """Every refresh calls ``apply``, even when the routing table is unchanged.

    A periodic loop that swallowed no-op refreshes would leave a recovered
    endpoint behind, because the apply callback is the only path the gateway
    learns the verdict from. Letting ``use_registry`` short-circuit on equality
    is what makes this cheap.
    """
    baseline = _registry_for("https://qwen.example")
    probe = ScriptedProbe(
        scripts={
            "https://qwen.example/v1": [True, True],
            "https://api.deepseek.com/v1": [True, True],
        }
    )
    applied_log, apply = _applied()

    monitor = EndpointHealthMonitor(baseline, probe, failures_to_demote=1, apply=apply)
    await monitor.refresh()
    await monitor.refresh()

    assert len(applied_log) == 2


def _peek(seq: Sequence[ResolvedDeployment], alias: str, api_base: str) -> ResolvedDeployment:
    for deployment in seq:
        if deployment.alias == alias and deployment.api_base == api_base:
            return deployment
    raise AssertionError(f"no deployment for alias={alias} base={api_base}")


def test_with_demoted_endpoints_preserves_fallbacks_and_skipped_aliases() -> None:
    """The demoted copy must not silently drop other registry metadata."""
    baseline = ModelRegistry(
        version=1,
        deployments=(
            _qwen("https://qwen.example", alias="factory-fast", priority=1),
            _deepseek(alias="factory-fast", priority=2),
        ),
        fallbacks={"factory-fast": ("factory-summary",)},
        skipped_aliases=("factory-summary",),
    )

    demoted = baseline.with_demoted_endpoints({"https://qwen.example/v1"})

    assert demoted.fallbacks == {"factory-fast": ("factory-summary",)}
    assert demoted.skipped_aliases == ("factory-summary",)
    assert _peek(demoted.deployments, "factory-fast", "https://api.deepseek.com/v1").priority == 1
    assert _peek(demoted.deployments, "factory-fast", "https://qwen.example/v1").priority == 2
