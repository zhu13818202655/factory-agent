"""Out-of-band LLM endpoint health probing against real upstream processes.

``tests/unit/llm/test_health.py`` drives a scripted fake; this suite runs the
genuine ``httpx.AsyncClient`` against actual servers so the wire path the
monitor depends on is the one we ship.
"""

from collections.abc import Iterator

import pytest

from factory_agent.llm.health import EndpointHealthMonitor, HttpModelsProbe
from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment
from factory_agent.llm.router_gateway import LiteLlmRouterGateway
from factory_agent.ports.model import (
    ModelMessage,
    ModelRequest,
    ModelStage,
)
from tests.support.llm_upstream import (
    TemporaryUpstream,
    temporary_upstream,
    unreachable_base_url,
)

CANARY_KEY = "sk-canary"


@pytest.fixture(scope="module")
def healthy() -> Iterator[TemporaryUpstream]:
    with temporary_upstream("ok", label="healthy") as running:
        yield running


@pytest.fixture(scope="module")
def broken() -> Iterator[TemporaryUpstream]:
    with temporary_upstream("server_error", label="broken") as running:
        yield running


def _upstream_deployment(
    upstream: TemporaryUpstream, alias: str, priority: int = 1
) -> ResolvedDeployment:
    """A deployment shaped like the integration tests in ``test_llm_routing``.

    Using ``model=f"openai/{upstream.label}"`` with no explicit provider and
    ``api_base=upstream.base_url`` (no ``/v1`` suffix) is the combination
    ``litellm.Router`` already accepts end-to-end.
    """
    return ResolvedDeployment(
        alias=alias,
        model=f"openai/{upstream.label}",
        api_base=upstream.base_url,
        api_key=CANARY_KEY,
        priority=priority,
    )


def _dead_qwen_deployment(endpoint: str, alias: str, priority: int = 1) -> ResolvedDeployment:
    """A Qwen-shaped deployment for the probe, which only needs api_base/api_key."""
    return ResolvedDeployment(
        alias=alias,
        model="Qwen/Qwen3.8-27B-FP8",
        api_base=f"{endpoint}/v1",
        api_key=CANARY_KEY,
        priority=priority,
        provider="openai",
    )


def _deepseek_deployment(alias: str, priority: int = 2) -> ResolvedDeployment:
    return ResolvedDeployment(
        alias=alias,
        model="deepseek/deepseek-chat",
        api_base="https://api.deepseek.com/v1",
        api_key=CANARY_KEY,
        priority=priority,
    )


def _registry_with(broken: TemporaryUpstream, healthy: TemporaryUpstream) -> ModelRegistry:
    """A single alias: ``broken`` at order 1, ``healthy`` at order 2."""
    return ModelRegistry(
        version=1,
        deployments=(
            _upstream_deployment(broken, alias="factory-fast", priority=1),
            _upstream_deployment(healthy, alias="factory-fast", priority=2),
        ),
        fallbacks={"factory-fast": ()},
    )


def _request() -> ModelRequest:
    return ModelRequest(
        model_alias="factory-fast",
        messages=(ModelMessage(role="user", content="上个月产量"),),
        stage=ModelStage.EXTRACT,
        logical_call_id="call-1",
        json_output=True,
    )


@pytest.mark.asyncio
async def test_probe_marks_a_real_upstream_healthy(healthy: TemporaryUpstream) -> None:
    probe = HttpModelsProbe(timeout_seconds=2.0)
    try:
        deployment = _dead_qwen_deployment(healthy.base_url, alias="factory-fast")
        assert await probe.is_healthy(deployment) is True
    finally:
        await probe.aclose()


@pytest.mark.asyncio
async def test_probe_marks_an_unreachable_endpoint_unhealthy() -> None:
    probe = HttpModelsProbe(timeout_seconds=1.0)
    try:
        deployment = _dead_qwen_deployment(unreachable_base_url(), alias="factory-fast")
        assert await probe.is_healthy(deployment) is False
    finally:
        await probe.aclose()


@pytest.mark.asyncio
async def test_probe_marks_a_500_responding_endpoint_unhealthy(broken: TemporaryUpstream) -> None:
    probe = HttpModelsProbe(timeout_seconds=2.0)
    try:
        deployment = _dead_qwen_deployment(broken.base_url, alias="factory-fast")
        assert await probe.is_healthy(deployment) is False
    finally:
        await probe.aclose()


@pytest.mark.asyncio
async def test_refresh_demotes_a_dead_endpoint_and_routes_around_it(
    broken: TemporaryUpstream, healthy: TemporaryUpstream
) -> None:
    """The end-to-end point: after one refresh the next call no longer pays the
    failed attempt against the dead endpoint, and ``fallback_reason`` flips from
    ``"fallback"`` to ``None`` because order-1 now answers on the first try.
    """
    baseline = _registry_with(broken=broken, healthy=healthy)
    probe = HttpModelsProbe(timeout_seconds=2.0)
    try:
        gateway_impl = LiteLlmRouterGateway(baseline, num_retries=0, default_timeout_seconds=5.0)
        monitor = EndpointHealthMonitor(
            baseline, probe, apply=gateway_impl.use_registry, failures_to_demote=1
        )

        # Without the monitor, the broken order-1 forces litellm into fallback.
        before = await gateway_impl.complete(_request())  # pyright: ignore[reportGeneralTypeIssues]
        assert before.fallback_reason == "fallback"

        await monitor.refresh()

        after = await gateway_impl.complete(_request())  # pyright: ignore[reportGeneralTypeIssues]
        assert after.fallback_reason is None
        assert monitor.demoted_endpoints == frozenset({broken.base_url})
    finally:
        await probe.aclose()


@pytest.mark.asyncio
async def test_refresh_restores_an_endpoint_that_comes_back(
    healthy: TemporaryUpstream,
) -> None:
    """A server that has come back answers its next probe and clears the demotion.

    The wire loop (broken → fixed) is exercised by spinning up a fresh monitor
    over a baseline whose prio-1 is the healthy endpoint, which is the state
    the user is waiting on once the 3090 host is fixed.
    """
    recovered_baseline = ModelRegistry(
        version=1,
        deployments=(
            _upstream_deployment(healthy, alias="factory-fast", priority=1),
            _deepseek_deployment(alias="factory-fast", priority=2),
        ),
        fallbacks={"factory-fast": ()},
    )
    probe = HttpModelsProbe(timeout_seconds=2.0)
    try:
        fresh = EndpointHealthMonitor(recovered_baseline, probe, apply=lambda _: None)
        await fresh.refresh()

        assert fresh.demoted_endpoints == frozenset()
    finally:
        await probe.aclose()
