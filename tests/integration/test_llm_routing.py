"""LiteLLM Router behaviour against real temporary upstream processes.

Unit tests drive a stub router; this suite runs the genuine ``litellm.router``
against actual servers, because the whole reason for adopting the SDK (ADR-0006)
is its reliability layer. Asserting fallback with a mocked router would prove
nothing about the thing we adopted it for.

An always-failing upstream is started next to a healthy one, so a fallback here
really crosses a process boundary. Nothing reaches the public network.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment
from factory_agent.llm.router_gateway import LiteLlmRouterGateway
from factory_agent.ports import (
    ModelErrorCategory,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelStage,
)
from tests.support.llm_upstream import TemporaryUpstream, temporary_upstream

FAST = "factory-fast"
REASONING = "factory-reasoning"


@pytest.fixture(scope="module")
def healthy() -> Iterator[TemporaryUpstream]:
    with temporary_upstream("ok", label="healthy") as running:
        yield running


@pytest.fixture(scope="module")
def broken() -> Iterator[TemporaryUpstream]:
    with temporary_upstream("server_error", label="broken") as running:
        yield running


def deployment(alias: str, upstream: TemporaryUpstream, priority: int = 1) -> ResolvedDeployment:
    return ResolvedDeployment(
        alias=alias,
        model=f"openai/{upstream.label}",
        api_base=upstream.base_url,
        api_key="test-key",
        priority=priority,
    )


def build(
    deployments: tuple[ResolvedDeployment, ...],
    fallbacks: dict[str, tuple[str, ...]] | None = None,
    *,
    num_retries: int = 0,
    timeout: float = 10.0,
) -> LiteLlmRouterGateway:
    registry = ModelRegistry(
        version=1,
        deployments=deployments,
        fallbacks=fallbacks or {item.alias: () for item in deployments},
    )
    return LiteLlmRouterGateway(
        registry,
        num_retries=num_retries,
        default_timeout_seconds=timeout,
    )


def request_for(alias: str = FAST) -> ModelRequest:
    return ModelRequest(
        model_alias=alias,
        messages=(ModelMessage(role="user", content="上个月产量"),),
        stage=ModelStage.EXTRACT,
        logical_call_id="call-1",
        json_output=True,
    )


@pytest.mark.asyncio
async def test_a_healthy_alias_round_trips_through_the_router(
    healthy: TemporaryUpstream,
) -> None:
    gateway = build((deployment(FAST, healthy),))

    response = await gateway.complete(request_for())

    assert '"capability_id"' in response.content
    assert response.usage.prompt_tokens == 11
    assert response.duration_ms >= 0


@pytest.mark.asyncio
async def test_a_failing_deployment_falls_over_to_a_healthy_peer(
    broken: TemporaryUpstream, healthy: TemporaryUpstream
) -> None:
    """The point of adopting the SDK: order=1 fails, order=2 serves the answer."""
    gateway = build(
        (deployment(FAST, broken, priority=1), deployment(FAST, healthy, priority=2)),
        num_retries=1,
    )

    response = await gateway.complete(request_for())

    assert response.actual_model == "healthy"
    assert '"capability_id"' in response.content


@pytest.mark.asyncio
async def test_a_failing_alias_falls_back_to_another_alias(
    broken: TemporaryUpstream, healthy: TemporaryUpstream
) -> None:
    gateway = build(
        (deployment(FAST, broken), deployment(REASONING, healthy)),
        fallbacks={FAST: (REASONING,), REASONING: ()},
        num_retries=1,
    )

    response = await gateway.complete(request_for(FAST))

    assert response.actual_model == "healthy"
    assert response.fallback_reason == "fallback"


@pytest.mark.asyncio
async def test_every_deployment_failing_surfaces_a_structured_error(
    broken: TemporaryUpstream,
) -> None:
    gateway = build((deployment(FAST, broken),))

    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete(request_for())

    assert caught.value.category in {
        ModelErrorCategory.UNAVAILABLE,
        ModelErrorCategory.RATE_LIMITED,
    }


@pytest.mark.asyncio
async def test_an_unreachable_deployment_is_unavailable_not_a_crash() -> None:
    registry = ModelRegistry(
        version=1,
        deployments=(
            ResolvedDeployment(
                alias=FAST,
                model="openai/gone",
                api_base="http://127.0.0.1:1/v1",
                api_key="test-key",
                priority=1,
            ),
        ),
        fallbacks={FAST: ()},
    )
    gateway = LiteLlmRouterGateway(registry, num_retries=0, default_timeout_seconds=5.0)

    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete(request_for())

    assert caught.value.category is ModelErrorCategory.UNAVAILABLE


@pytest.mark.asyncio
async def test_router_errors_never_leak_the_provider_key_or_prompt(
    broken: TemporaryUpstream,
) -> None:
    gateway = build((deployment(FAST, broken),))

    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete(request_for())

    rendered = str(caught.value)
    assert "test-key" not in rendered
    assert "上个月产量" not in rendered
