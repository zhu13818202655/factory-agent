

from dataclasses import dataclass, field
from typing import Any, cast

import litellm
import pytest
from litellm.exceptions import Timeout

from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment
from factory_agent.llm.router_gateway import (
    LiteLlmRouterGateway,
    silence_litellm_global_state,
)
from factory_agent.ports.model import (
    ModelDeltaKind,
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


@pytest.mark.asyncio
async def test_use_registry_swaps_the_routing_table_when_order_changes() -> None:
    """A demotion from the health monitor must rebuild litellm's Router.

    The stub router lets us assert the swap without a network, by checking
    that the gateway publishes the demoted registry on its own handle. The
    deeper proof (the new order actually wins at completion time) lives in
    ``tests/integration/test_llm_health.py``.
    """
    router = StubRouter()
    two_tier = ModelRegistry(
        version=1,
        deployments=(
            ResolvedDeployment(
                alias="factory-fast",
                model="Qwen/Qwen3.8-27B-FP8",
                api_base="http://qwen.example/v1",
                api_key=CANARY_KEY,
                priority=1,
                provider="openai",
            ),
            ResolvedDeployment(
                alias="factory-fast",
                model="deepseek/deepseek-chat",
                api_base="https://api.deepseek.com/v1",
                api_key=CANARY_KEY,
                priority=2,
            ),
        ),
        fallbacks={"factory-fast": ()},
    )
    built = gateway(router, reg=two_tier)
    demoted = built._registry.with_demoted_endpoints(  # pyright: ignore[reportPrivateUsage]
        {"http://qwen.example/v1"}
    )

    built.use_registry(demoted)

    assert built._registry is demoted  # pyright: ignore[reportPrivateUsage]
    assert built._router is not router  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_use_registry_is_a_no_op_when_the_verdict_changes_nothing() -> None:
    router = StubRouter()
    built = gateway(router)
    original_router = built._router  # pyright: ignore[reportPrivateUsage]

    built.use_registry(built._registry)  # pyright: ignore[reportPrivateUsage]

    assert built._router is original_router  # pyright: ignore[reportPrivateUsage]


# --------------------------------------------------------------------------- #
# streaming: the narration path (``ModelStreamGateway``)
# --------------------------------------------------------------------------- #


@dataclass
class StreamingRouter:
    """Stands in for ``litellm.acompletion(stream=True)``.

    The real call returns an async iterable of chunks rather than a body, so a
    double that returns a dict cannot exercise the streaming path at all.
    """

    chunks: list[dict[str, Any]] = field(default_factory=lambda: [])
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=lambda: [])

    async def acompletion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _chunks(self.chunks)


async def _chunks(payloads: list[dict[str, Any]]) -> Any:
    for payload in payloads:
        yield payload


def chunk(
    content: str | None = None,
    *,
    reasoning: str | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    """One OpenAI-compatible streamed chunk."""
    if not choices:
        return {"choices": [], "usage": {"completion_tokens": 7}}
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "delta": delta}]}


def streaming_gateway(
    router: StreamingRouter,
    *aliases: str,
    thinking_enabled: bool = False,
    reg: ModelRegistry | None = None,
) -> LiteLlmRouterGateway:
    return LiteLlmRouterGateway(
        reg or registry(*aliases),
        router=router,  # pyright: ignore[reportArgumentType]
        thinking_enabled=thinking_enabled,
        thinking_effort="high",
    )


async def collect(
    built: LiteLlmRouterGateway, alias: str = "factory-fast"
) -> list[tuple[str, ModelDeltaKind]]:
    deltas = [
        delta
        async for delta in built.stream(
            ModelRequest(
                model_alias=alias,
                messages=(ModelMessage(role="user", content=CANARY_PROMPT),),
                stage=ModelStage.THINKING,
                logical_call_id="call-1",
            )
        )
    ]
    return [(delta.text, delta.kind) for delta in deltas]


@pytest.mark.asyncio
async def test_stream_asks_the_router_for_a_stream() -> None:
    router = StreamingRouter(chunks=[chunk("正在取数。")])

    await collect(streaming_gateway(router))

    assert router.calls[0]["stream"] is True
    # Same single call boundary as ``complete``: the logical alias, never a
    # provider model name.
    assert router.calls[0]["model"] == "factory-fast"


@pytest.mark.asyncio
async def test_stream_withholds_provider_thinking_fields() -> None:
    """With thinking on, a streamed call would spend the whole stream on
    deliberation and only emit visible text at the end — a stall to anyone
    watching the narration block."""
    router = StreamingRouter(chunks=[chunk("正在取数。")])

    await collect(streaming_gateway(router, thinking_enabled=True))

    assert "extra_body" not in router.calls[0]


@pytest.mark.asyncio
async def test_a_completed_call_still_sends_the_provider_thinking_fields() -> None:
    """The withholding is specific to streaming, not a global change."""
    router = StubRouter()

    await gateway(router, thinking_enabled=True).complete(request())

    assert router.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_stream_surfaces_content_and_keeps_deliberation_distinguishable() -> None:
    router = StreamingRouter(
        chunks=[
            chunk(),  # role-only opening chunk
            chunk(reasoning="让我先想一下"),
            # A real stream never mixes the two channels in one chunk; when a
            # chunk carries deliberation, that is what it is for.
            chunk("正在核对"),
            chunk("权限范围。"),
            chunk("", choices=False),  # trailing usage-only chunk
        ]
    )

    deltas = await collect(streaming_gateway(router))

    assert deltas == [
        ("让我先想一下", ModelDeltaKind.REASONING),
        ("正在核对", ModelDeltaKind.CONTENT),
        ("权限范围。", ModelDeltaKind.CONTENT),
    ]


@pytest.mark.asyncio
async def test_deliberation_wins_over_content_within_one_chunk() -> None:
    """Documented precedence: a chunk that carries ``reasoning_content`` is
    read as deliberation, so the two channels never interleave out of order."""
    router = StreamingRouter(chunks=[chunk("正文", reasoning="思考")])

    assert await collect(streaming_gateway(router)) == [("思考", ModelDeltaKind.REASONING)]


@pytest.mark.asyncio
async def test_stream_maps_a_provider_failure_to_a_category() -> None:
    # ``litellm.exceptions.Timeout`` is the same class the package re-exports as
    # ``litellm.Timeout``; importing it from its home module is what pyright can
    # resolve (the re-export is invisible to it).
    router = StreamingRouter(error=Timeout(message="boom", model="m", llm_provider="p"))

    with pytest.raises(ModelGatewayError) as caught:
        await collect(streaming_gateway(router))

    assert caught.value.category is ModelErrorCategory.TIMEOUT


@pytest.mark.asyncio
async def test_stream_failures_never_echo_prompt_or_provider_detail() -> None:
    router = StreamingRouter(error=RuntimeError(f"stream died with {CANARY_PROMPT} {CANARY_KEY}"))

    with pytest.raises(ModelGatewayError) as caught:
        await collect(streaming_gateway(router))

    rendered = f"{caught.value}{caught.value.message}"
    assert CANARY_PROMPT not in rendered
    assert CANARY_KEY not in rendered


@pytest.mark.asyncio
async def test_stream_refuses_an_unconfigured_alias_at_the_call_site() -> None:
    """Eager validation: an alias with no deployment fails where narration was
    requested, not on first iteration deep inside a task."""
    router = StreamingRouter(chunks=[chunk("正在取数。")])
    built = streaming_gateway(router, "factory-summary")

    with pytest.raises(ModelGatewayError) as caught:
        built.stream(
            ModelRequest(
                model_alias="factory-fast",
                messages=(ModelMessage(role="user", content=CANARY_PROMPT),),
                stage=ModelStage.THINKING,
                logical_call_id="call-1",
            )
        )

    assert caught.value.category is ModelErrorCategory.NOT_CONFIGURED
    assert router.calls == []


@pytest.mark.asyncio
async def test_debug_capture_records_llm_request_and_response() -> None:
    """B-channel: LLM 调用的输入（消息序列+采样参数）与输出（正文+用量）入捕获库。

    修复前 LLM 调用完全没有捕获点 —— 报告里 LLM 行只有「未捕获载荷」。
    输入输出必须保持 JSON 原生结构（messages 是列表），供报告树状渲染。
    """
    import json as _json

    from factory_agent.observability.debug_trace import (
        CaptureScope,
        close_capture_scope,
        configure_debug_trace,
        drain_debug_captures,
        open_capture_scope,
    )
    from tests.support.payload import as_dict, as_list

    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(
        CaptureScope(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-1",
            interaction_id="interaction-1",
        )
    )
    try:
        built = gateway(StubRouter())
        response = await built.complete(request())
    finally:
        captures = drain_debug_captures()
        close_capture_scope()
        configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)

    assert response.content == '{"ok": true}'
    llm = [c for c in captures if c.kind == "llm"]
    assert len(llm) == 1, f"expected exactly one llm capture, got {len(llm)}"
    capture = llm[0]
    assert capture.stage == "classify"
    assert capture.logical_call_id == "call-1"

    payload = capture.payload
    assert payload.truncated is False
    inp = as_dict(payload.input)
    assert inp["model_alias"] == "factory-fast"
    messages = as_list(inp["messages"])
    assert len(messages) == 1
    assert as_dict(messages[0])["content"] == CANARY_PROMPT

    out = as_dict(payload.output)
    assert out["content"] == '{"ok": true}'
    usage = as_dict(out["usage"])
    assert usage["prompt_tokens"] == 31
    # JSON 原生结构（不是 repr 字符串）：报告可以树状渲染
    assert "'content'" not in _json.dumps(payload.output, ensure_ascii=False)
    assert '"content"' in _json.dumps(payload.output, ensure_ascii=False)


@pytest.mark.asyncio
async def test_debug_capture_records_streamed_reply_as_accumulated_text() -> None:
    """流式调用在流结束后整体入捕获：content/reasoning 拼接为完整文本。"""
    from factory_agent.observability.debug_trace import (
        CaptureScope,
        close_capture_scope,
        configure_debug_trace,
        drain_debug_captures,
        open_capture_scope,
    )
    from tests.support.payload import as_dict

    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(
        CaptureScope(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-1",
            interaction_id="interaction-1",
        )
    )
    try:
        built = streaming_gateway(
            StreamingRouter(chunks=[chunk(reasoning="思考中。"), chunk("全厂"), chunk("合计。")]),
            "factory-fast",
        )
        parts = [
            delta.text
            async for delta in built.stream(
                ModelRequest(
                    model_alias="factory-fast",
                    messages=(ModelMessage(role="user", content=CANARY_PROMPT),),
                    stage=ModelStage.SUMMARIZE,
                    logical_call_id="call-stream-1",
                )
            )
            if delta.kind is ModelDeltaKind.CONTENT
        ]
    finally:
        captures = drain_debug_captures()
        close_capture_scope()
        configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)

    assert "".join(parts) == "全厂合计。"
    llm = [c for c in captures if c.kind == "llm"]
    assert len(llm) == 1
    out = as_dict(llm[0].payload.output)
    assert out["streamed"] is True
    assert out["content"] == "全厂合计。"
    assert out["reasoning"] == "思考中。"
