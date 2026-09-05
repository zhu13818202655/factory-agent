from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import litellm
import pytest

from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment
from factory_agent.llm.router_gateway import (
    LiteLlmRouterGateway,
    silence_litellm_global_state,
)
from factory_agent.ports.model import (
    ModelErrorCategory,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelStage,
)

CANARY_PROMPT = "员工 A-1 上个月工资 8123.45 元"
CANARY_KEY = "sk-canary-key"


@dataclass
class StubRouter:
    """Stands in for ``litellm.router.Router`` at the single call boundary."""

    body: dict[str, Any] | None = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=lambda: [])

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return completion() if self.body is None else self.body


def completion(
    content: str = '{"ok": true}',
    *,
    model: str = "deepseek/deepseek-chat",
    model_group: str | None = None,
    attempted_retries: int = 0,
    attempted_fallbacks: int = 0,
) -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": 31,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
        "_hidden_params": {
            "additional_headers": {
                "x-litellm-attempted-retries": attempted_retries,
                "x-litellm-attempted-fallbacks": attempted_fallbacks,
                "x-litellm-model-group": model_group,
            }
        },
    }


def registry(*aliases: str) -> ModelRegistry:
    names = aliases or ("factory-fast",)
    return ModelRegistry(
        version=1,
        deployments=tuple(
            ResolvedDeployment(
                alias=name,
                model="deepseek/deepseek-chat",
                api_base="https://api.deepseek.com/v1",
                api_key=CANARY_KEY,
                priority=1,
            )
            for name in names
        ),
        fallbacks={name: () for name in names},
    )


def qwen_registry(*aliases: str) -> ModelRegistry:
    """A Qwen3/vLLM-family alias (self-hosted OpenAI-compatible endpoint)."""
    names = aliases or ("factory-fast",)
    return ModelRegistry(
        version=1,
        deployments=tuple(
            ResolvedDeployment(
                alias=name,
                model="Qwen/Qwen3.8-27B-FP8",
                api_base="http://117.184.148.14:21003/v1",
                api_key=CANARY_KEY,
                priority=1,
                provider="openai",
            )
            for name in names
        ),
        fallbacks={name: () for name in names},
    )


def gateway(
    router: StubRouter,
    *aliases: str,
    thinking_enabled: bool = False,
    thinking_effort: str = "high",
    reg: ModelRegistry | None = None,
) -> LiteLlmRouterGateway:
    return LiteLlmRouterGateway(
        reg or registry(*aliases),
        router=router,  # pyright: ignore[reportArgumentType]
        thinking_enabled=thinking_enabled,
        thinking_effort=thinking_effort,
    )


def request(alias: str = "factory-fast", *, json_output: bool = True) -> ModelRequest:
    return ModelRequest(
        model_alias=alias,
        messages=(ModelMessage(role="user", content=CANARY_PROMPT),),
        stage=ModelStage.CLASSIFY,
        logical_call_id="call-1",
        json_output=json_output,
    )


@pytest.mark.asyncio
async def test_request_uses_the_logical_alias_not_a_provider_model() -> None:
    router = StubRouter()

    await gateway(router).complete(request())

    assert router.calls[0]["model"] == "factory-fast"


@pytest.mark.asyncio
async def test_json_output_requests_a_json_object() -> None:
    router = StubRouter()

    await gateway(router).complete(request())

    assert router.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_plain_output_does_not_force_a_response_format() -> None:
    router = StubRouter()

    await gateway(router).complete(request(json_output=False))

    assert "response_format" not in router.calls[0]


@pytest.mark.asyncio
async def test_response_reports_actual_model_tokens_and_attempts() -> None:
    router = StubRouter(body=completion(model="deepseek/deepseek-reasoner", attempted_retries=2))

    response = await gateway(router).complete(request())

    assert response.actual_model == "deepseek/deepseek-reasoner"
    assert response.attempt == 3
    assert response.usage.prompt_tokens == 31
    assert response.usage.cached_tokens == 5
    assert response.usage.reasoning_tokens == 3


@pytest.mark.asyncio
async def test_a_served_group_matching_the_alias_is_not_a_fallback() -> None:
    router = StubRouter(body=completion(model_group="factory-fast"))

    response = await gateway(router).complete(request())

    assert response.fallback_reason is None


@pytest.mark.asyncio
async def test_a_different_served_group_is_recorded_as_a_fallback() -> None:
    router = StubRouter(body=completion(model_group="factory-reasoning"))

    response = await gateway(router).complete(request())

    assert response.fallback_reason == "fallback"


@pytest.mark.asyncio
async def test_a_counted_fallback_is_recorded_even_for_the_same_group() -> None:
    router = StubRouter(body=completion(model_group="factory-fast", attempted_fallbacks=1))

    response = await gateway(router).complete(request())

    assert response.fallback_reason == "fallback"


@pytest.mark.asyncio
async def test_an_unconfigured_alias_fails_before_any_call() -> None:
    router = StubRouter()

    with pytest.raises(ModelGatewayError) as caught:
        await gateway(router).complete(request("factory-ghost"))

    assert caught.value.category is ModelErrorCategory.NOT_CONFIGURED
    assert router.calls == []


@pytest.mark.asyncio
async def test_empty_messages_are_refused_before_any_call() -> None:
    router = StubRouter()
    empty = ModelRequest(
        model_alias="factory-fast",
        messages=(),
        stage=ModelStage.CLASSIFY,
        logical_call_id="call-1",
    )

    with pytest.raises(ModelGatewayError):
        await gateway(router).complete(empty)

    assert router.calls == []


@pytest.mark.parametrize(
    ("exception_name", "expected"),
    [
        ("Timeout", ModelErrorCategory.TIMEOUT),
        ("RateLimitError", ModelErrorCategory.RATE_LIMITED),
        ("AuthenticationError", ModelErrorCategory.UNAUTHENTICATED),
        ("ServiceUnavailableError", ModelErrorCategory.UNAVAILABLE),
        ("InternalServerError", ModelErrorCategory.UNAVAILABLE),
        ("APIConnectionError", ModelErrorCategory.UNAVAILABLE),
        ("BadRequestError", ModelErrorCategory.PROTOCOL),
        ("ContextWindowExceededError", ModelErrorCategory.PROTOCOL),
    ],
)
@pytest.mark.asyncio
async def test_litellm_exceptions_map_to_categories(
    exception_name: str, expected: ModelErrorCategory
) -> None:
    raised = type(exception_name, (Exception,), {})
    router = StubRouter(error=raised("upstream detail"))

    with pytest.raises(ModelGatewayError) as caught:
        await gateway(router).complete(request())

    assert caught.value.category is expected


@pytest.mark.asyncio
async def test_an_unrecognized_exception_is_treated_as_unavailable() -> None:
    router = StubRouter(error=RuntimeError("something new"))

    with pytest.raises(ModelGatewayError) as caught:
        await gateway(router).complete(request())

    assert caught.value.category is ModelErrorCategory.UNAVAILABLE


@pytest.mark.asyncio
async def test_gateway_errors_never_echo_prompt_or_provider_detail() -> None:
    router = StubRouter(error=RuntimeError(f"{CANARY_PROMPT} using key {CANARY_KEY}"))

    with pytest.raises(ModelGatewayError) as caught:
        await gateway(router).complete(request())

    rendered = str(caught.value)
    assert CANARY_PROMPT not in rendered
    assert CANARY_KEY not in rendered


UNUSABLE_BODIES: list[dict[str, Any]] = [
    {"choices": []},
    {"choices": [{"message": {}}]},
    {"choices": [{"message": {"content": "   "}}]},
    {"choices": "not-a-list"},
    {},
]


@pytest.mark.parametrize("body", UNUSABLE_BODIES)
@pytest.mark.asyncio
async def test_unusable_bodies_are_protocol_failures(body: dict[str, Any]) -> None:
    router = StubRouter(body=body)

    with pytest.raises(ModelGatewayError) as caught:
        await gateway(router).complete(request())

    assert caught.value.category is ModelErrorCategory.PROTOCOL


@pytest.mark.asyncio
async def test_missing_usage_defaults_to_zero_rather_than_failing() -> None:
    router = StubRouter(body={"choices": [{"message": {"content": "ok"}}]})

    response = await gateway(router).complete(request())

    assert response.usage.prompt_tokens == 0
    assert response.content == "ok"


def test_construction_disables_litellm_prompt_logging() -> None:
    litellm.turn_off_message_logging = False
    litellm.callbacks = ["noisy"]

    silence_litellm_global_state()

    assert litellm.turn_off_message_logging is True
    assert litellm.callbacks == []
    assert len(cast("list[object]", litellm.success_callback)) == 0  # pyright: ignore[reportUnknownMemberType]
    assert len(cast("list[object]", litellm.failure_callback)) == 0  # pyright: ignore[reportUnknownMemberType]


# ---------------------------------------------------------------------------
# Thinking-mode policy (global, default OFF; enabled effort defaults to high)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_is_off_by_default_and_deepseek_gets_explicit_disable() -> None:
    router = StubRouter()

    await gateway(router).complete(request())

    # DeepSeek-family registry is the unit default; thinking OFF must be sent
    # explicitly because DeepSeek servers default thinking to ON.
    assert router.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_thinking_enabled_defaults_effort_to_high_for_deepseek() -> None:
    router = StubRouter()

    await gateway(router, thinking_enabled=True).complete(request())

    assert router.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_deepseek_effort_medium_is_mapped_to_high() -> None:
    router = StubRouter()

    await gateway(router, thinking_enabled=True, thinking_effort="medium").complete(request())

    assert router.calls[0]["extra_body"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_deepseek_effort_max_is_kept() -> None:
    router = StubRouter()

    await gateway(router, thinking_enabled=True, thinking_effort="max").complete(request())

    assert router.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


@pytest.mark.asyncio
async def test_qwen_family_thinking_off_uses_chat_template_kwargs() -> None:
    router = StubRouter()

    await gateway(router, reg=qwen_registry()).complete(request())

    assert router.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.asyncio
async def test_qwen_family_thinking_on_maps_effort_and_caps_max_to_high() -> None:
    router = StubRouter()

    await gateway(
        router, thinking_enabled=True, thinking_effort="max", reg=qwen_registry()
    ).complete(request())

    assert router.calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True, "thinking_effort": "high"}
    }
