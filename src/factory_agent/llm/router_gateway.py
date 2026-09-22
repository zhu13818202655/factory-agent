"""LiteLLM Router gateway (ADR-0006).

The product's only outbound LLM boundary. Business code names a logical alias;
the router owns deployment selection, ordered fallback, backoff and cooldown.

Two litellm behaviours are neutralized here on purpose:

* global verbose logging and callbacks are disabled, because both receive full
  prompts and would violate the sensitive-data invariant;
* implicit environment credential pickup is bypassed, because every key must
  come from the reviewed registry.

``Router`` is imported from ``litellm.router`` rather than ``litellm``: the
top-level package does not re-export it, and Pyright strict rejects the
re-export as a private import.
"""

import time
from collections.abc import AsyncIterator
from typing import Any, cast

import litellm
from litellm.router import Router

from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment
from factory_agent.observability.debug_trace import (
    debug_capture_enabled,
    llm_span_key,
    record_debug_capture,
)
from factory_agent.ports.model import (
    ModelDelta,
    ModelDeltaKind,
    ModelErrorCategory,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)

_JSON_OBJECT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}

# Thinking-mode wire formats are provider-specific although both providers are
# OpenAI-compatible:
#   * Qwen3 / vLLM family:  chat_template_kwargs {enable_thinking, thinking_effort}
#   * DeepSeek family:      thinking.type (enabled/disabled) + reasoning_effort
# Product policy speaks one vocabulary (low/medium/high/max); each family maps
# it to what its server accepts. DeepSeek official mapping (flash == pro):
# low→low, medium→high, high→high, xhigh→high, max→max. Qwen only supports
# low/medium/high, so max→high.
_DEEPSEEK_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
}
_QWEN_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}

# litellm exception class names mapped to our categories. Matching by name keeps
# this table readable and avoids importing litellm's exception module, whose
# membership shifts between releases.
_ERROR_CATEGORIES: dict[str, ModelErrorCategory] = {
    "Timeout": ModelErrorCategory.TIMEOUT,
    "APITimeoutError": ModelErrorCategory.TIMEOUT,
    "RateLimitError": ModelErrorCategory.RATE_LIMITED,
    "AuthenticationError": ModelErrorCategory.UNAUTHENTICATED,
    "PermissionDeniedError": ModelErrorCategory.UNAUTHENTICATED,
    "ServiceUnavailableError": ModelErrorCategory.UNAVAILABLE,
    "InternalServerError": ModelErrorCategory.UNAVAILABLE,
    "APIConnectionError": ModelErrorCategory.UNAVAILABLE,
    "APIError": ModelErrorCategory.UNAVAILABLE,
    "BadRequestError": ModelErrorCategory.PROTOCOL,
    "UnprocessableEntityError": ModelErrorCategory.PROTOCOL,
    "ContextWindowExceededError": ModelErrorCategory.PROTOCOL,
    "NotFoundError": ModelErrorCategory.PROTOCOL,
}


def silence_litellm_global_state() -> None:
    """Stop litellm from logging prompts or calling out to global sinks."""
    litellm.set_verbose = False  # pyright: ignore[reportPrivateImportUsage]
    litellm.turn_off_message_logging = True
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    litellm.drop_params = True


class LiteLlmRouterGateway:
    """`ModelGateway` backed by ``litellm.router.Router``."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        default_timeout_seconds: float = 30.0,
        default_temperature: float = 0.0,
        default_top_p: float = 1.0,
        default_max_output_tokens: int = 2048,
        num_retries: int = 2,
        allowed_fails: int = 2,
        cooldown_seconds: int = 30,
        thinking_enabled: bool = False,
        thinking_effort: str = "high",
        router: Router | None = None,
    ) -> None:
        silence_litellm_global_state()
        self._registry = registry
        self._default_timeout_seconds = default_timeout_seconds
        self._default_temperature = default_temperature
        self._default_top_p = default_top_p
        self._default_max_output_tokens = default_max_output_tokens
        self._num_retries = num_retries
        self._allowed_fails = allowed_fails
        self._cooldown_seconds = cooldown_seconds
        self._thinking_enabled = thinking_enabled
        self._thinking_effort = (thinking_effort or "high").lower()
        self._router = router or self._build_router(registry)

    def use_registry(self, registry: ModelRegistry) -> None:
        """Adopt a routing table reordered by the endpoint health monitor.

        litellm's Router is built from a static ``model_list``, so a changed
        ``order`` means a new router; an unchanged one is left in place so a
        periodic probe never churns the connection pools.
        """
        if registry.deployments == self._registry.deployments:
            self._registry = registry
            return
        self._registry = registry
        self._router = self._build_router(registry)

    def _build_router(self, registry: ModelRegistry) -> Router:
        return Router(
            model_list=_model_list(registry),
            fallbacks=_fallbacks(registry),
            num_retries=self._num_retries,
            allowed_fails=self._allowed_fails,
            cooldown_time=self._cooldown_seconds,
            timeout=self._default_timeout_seconds,
            set_verbose=False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._validate(request)

        started = time.monotonic()
        try:
            raw = await self._acompletion(request)
        except Exception as exc:
            self._capture_error(request, exc)
            raise _translate(exc, _elapsed_ms(started)) from exc

        body = _as_mapping(raw)
        response = ModelResponse(
            content=_content(body, started),
            actual_model=_actual_model(body, request.model_alias),
            usage=_usage(body),
            duration_ms=_elapsed_ms(started),
            attempt=_attempt(body),
            fallback_reason=_fallback_reason(body, request.model_alias),
        )
        self._capture_response(request, response)
        return response

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]:
        """Incremental output for one call.

        Reasoning fields are deliberately NOT requested: a deployment with
        thinking enabled spends the whole stream on deliberation and only emits
        visible text at the very end, which reads as a stall to a user watching
        a thinking block. A caller that wants the model's own deliberation gets
        it by configuring the deployment, not by asking here.

        Validation runs eagerly (not on first iteration) so an unconfigured
        alias fails at the call site that requested narration.
        """
        self._validate(request)
        return self._stream(request)

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]:
        started = time.monotonic()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            chunks = await self._acompletion_stream(request)
            async for chunk in chunks:
                delta = _delta(chunk)
                if delta is not None:
                    bucket = (
                        reasoning_parts
                        if delta.kind is ModelDeltaKind.REASONING
                        else content_parts
                    )
                    bucket.append(delta.text)
                    yield delta
        except ModelGatewayError as exc:
            self._capture_stream_error(request, content_parts, reasoning_parts, exc)
            raise
        except Exception as exc:
            self._capture_stream_error(request, content_parts, reasoning_parts, exc)
            raise _translate(exc, _elapsed_ms(started)) from exc
        self._capture_stream(request, content_parts, reasoning_parts, _elapsed_ms(started))

    # ---- B-channel capture (ADR-0004) -------------------------------------
    # 同一个出口捕获请求与响应：输入是消息序列与采样参数，输出是最终文本与
    # 用量。脱敏与上限都在 capture 层做，这里只负责把结构喂进去——
    # pydantic/dataclass 一律先转 JSON 原生形态，避免 default=str 摊平。
    def _capture_input(self, request: ModelRequest) -> dict[str, object]:
        return {
            "model_alias": request.model_alias,
            "stage": getattr(request.stage, "value", str(request.stage)),
            "json_output": request.json_output,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_output_tokens": request.max_output_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }

    def _capture_response(self, request: ModelRequest, response: ModelResponse) -> None:
        if not debug_capture_enabled():
            return
        record_debug_capture(
            span_key=llm_span_key(request.logical_call_id),
            kind="llm",
            input_payload=self._capture_input(request),
            output_payload={
                "content": response.content,
                "actual_model": response.actual_model,
                "attempt": response.attempt,
                "fallback_reason": response.fallback_reason,
                "duration_ms": response.duration_ms,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "cached_tokens": response.usage.cached_tokens,
                    "reasoning_tokens": response.usage.reasoning_tokens,
                },
            },
            stage=getattr(request.stage, "value", str(request.stage)),
            logical_call_id=request.logical_call_id,
            attempt=response.attempt,
        )

    def _capture_stream(
        self,
        request: ModelRequest,
        content_parts: list[str],
        reasoning_parts: list[str],
        duration_ms: int,
    ) -> None:
        if not debug_capture_enabled():
            return
        record_debug_capture(
            span_key=llm_span_key(request.logical_call_id),
            kind="llm",
            input_payload=self._capture_input(request),
            output_payload={
                "content": "".join(content_parts),
                "reasoning": "".join(reasoning_parts),
                "streamed": True,
                "duration_ms": duration_ms,
            },
            stage=getattr(request.stage, "value", str(request.stage)),
            logical_call_id=request.logical_call_id,
        )

    def _capture_stream_error(
        self,
        request: ModelRequest,
        content_parts: list[str],
        reasoning_parts: list[str],
        exc: Exception,
    ) -> None:
        if not debug_capture_enabled():
            return
        record_debug_capture(
            span_key=llm_span_key(request.logical_call_id),
            kind="llm",
            input_payload=self._capture_input(request),
            output_payload={
                "error": type(exc).__name__,
                "message": str(exc),
                "partial_content": "".join(content_parts),
                "partial_reasoning": "".join(reasoning_parts),
                "streamed": True,
            },
            stage=getattr(request.stage, "value", str(request.stage)),
            logical_call_id=request.logical_call_id,
        )

    def _capture_error(self, request: ModelRequest, exc: Exception) -> None:
        if not debug_capture_enabled():
            return
        record_debug_capture(
            span_key=llm_span_key(request.logical_call_id),
            kind="llm",
            input_payload=self._capture_input(request),
            output_payload={"error": type(exc).__name__, "message": str(exc)},
            stage=getattr(request.stage, "value", str(request.stage)),
            logical_call_id=request.logical_call_id,
        )

    def _validate(self, request: ModelRequest) -> None:
        if not request.messages:
            raise ModelGatewayError(ModelErrorCategory.PROTOCOL, "messages cannot be empty")
        if request.model_alias not in self._registry.aliases():
            raise ModelGatewayError(
                ModelErrorCategory.NOT_CONFIGURED,
                f"alias {request.model_alias} has no configured deployment",
            )

    async def _acompletion(self, request: ModelRequest) -> object:
        """Sole litellm call site; its loose typing is contained here."""
        call = cast("Any", self._router.acompletion)  # pyright: ignore[reportUnknownMemberType]
        return await call(
            model=request.model_alias,
            messages=_messages(request),
            **self._call_options(request),
        )

    async def _acompletion_stream(self, request: ModelRequest) -> AsyncIterator[object]:
        """Streaming sibling of ``_acompletion``; same single call boundary."""
        call = cast("Any", self._router.acompletion)  # pyright: ignore[reportUnknownMemberType]
        return cast(
            "AsyncIterator[object]",
            await call(
                model=request.model_alias,
                messages=_messages(request),
                stream=True,
                **self._call_options(request, streaming=True),
            ),
        )

    def _call_options(self, request: ModelRequest, *, streaming: bool = False) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": (
                self._default_temperature if request.temperature is None else request.temperature
            ),
            "top_p": self._default_top_p if request.top_p is None else request.top_p,
            "max_tokens": request.max_output_tokens or self._default_max_output_tokens,
            "timeout": request.timeout_seconds or self._default_timeout_seconds,
        }
        if request.json_output:
            options["response_format"] = dict(_JSON_OBJECT_RESPONSE_FORMAT)
        # A streamed call must produce visible text as it arrives, so the
        # provider thinking fields are withheld: with thinking on, the deltas
        # are deliberation and the answer only lands at the end.
        if not streaming:
            thinking = self._thinking_body(request.model_alias)
            if thinking:
                options["extra_body"] = thinking
        return options

    def _thinking_body(self, alias: str) -> dict[str, Any]:
        """Provider-specific thinking request fields for one logical alias.

        The wire format must match the deployment litellm actually serves.
        The router routes by deployment priority first, so the format of the
        alias's highest-priority usable deployment is used. A mixed-family
        alias that falls over to a lower-priority provider of another family
        receives the primary family's fields, which the secondary server
        ignores (documented limitation of sharing one alias across providers).
        """
        if self._is_deepseek_alias(alias):
            if not self._thinking_enabled:
                return {"thinking": {"type": "disabled"}}
            return {
                "thinking": {"type": "enabled"},
                "reasoning_effort": _DEEPSEEK_EFFORT_MAP.get(self._thinking_effort, "high"),
            }
        if not self._thinking_enabled:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {
            "chat_template_kwargs": {
                "enable_thinking": True,
                "thinking_effort": _QWEN_EFFORT_MAP.get(self._thinking_effort, "high"),
            }
        }

    def _is_deepseek_alias(self, alias: str) -> bool:
        candidates = [item for item in self._registry.deployments if item.alias == alias]
        if not candidates:
            return False
        primary = min(candidates, key=lambda item: item.priority)
        return _is_deepseek_deployment(primary)


def _model_list(registry: ModelRegistry) -> list[dict[str, Any]]:
    return [
        {
            "model_name": deployment.alias,
            "litellm_params": {
                # Provider-qualified: an unqualified self-hosted id (e.g.
                # "Qwen/...") makes Router construction fail with "LLM Provider
                # NOT provided". See ResolvedDeployment.litellm_model.
                "model": deployment.litellm_model,
                "api_base": deployment.api_base,
                "api_key": deployment.api_key,
                "order": deployment.priority,
            },
        }
        for deployment in registry.deployments
    ]


def _fallbacks(registry: ModelRegistry) -> list[dict[str, list[str]]]:
    """Only keep fallback targets that actually resolved a deployment."""
    usable = registry.aliases()
    entries: list[dict[str, list[str]]] = []
    for alias, targets in registry.fallbacks.items():
        if alias not in usable:
            continue
        reachable = [target for target in targets if target in usable]
        if reachable:
            entries.append({alias: reachable})
    return entries


def _messages(request: ModelRequest) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in request.messages]


def _is_deepseek_deployment(deployment: ResolvedDeployment) -> bool:
    """True when a deployment targets the DeepSeek family.

    Env retargeting (model_env / api_base_env) can point an ``openai``-declared
    deployment at DeepSeek, so provider, model id, and api_base are all checked.
    """
    haystack = " ".join((deployment.provider or "", deployment.model, deployment.api_base)).lower()
    return "deepseek" in haystack


def _as_mapping(raw: object) -> dict[str, Any]:
    """Normalize litellm's response object to a dict.

    ``_hidden_params`` is an attribute rather than a model field, so
    ``model_dump`` drops it; it is read first and re-attached here because it
    carries the attempt and fallback facts usage metering needs.
    """
    hidden = cast("object", getattr(raw, "_hidden_params", None))

    body: dict[str, Any] | None = None
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        decoded: object = cast("Any", dump)()
        if isinstance(decoded, dict):
            body = cast("dict[str, Any]", decoded)
    if body is None and isinstance(raw, dict):
        body = cast("dict[str, Any]", raw)
    if body is None:
        raise ModelGatewayError(
            ModelErrorCategory.PROTOCOL, "router returned an unreadable response"
        )

    if isinstance(hidden, dict) and "_hidden_params" not in body:
        body["_hidden_params"] = cast("dict[str, Any]", hidden)
    return body


def _router_headers(body: dict[str, Any]) -> dict[str, Any]:
    hidden: object = body.get("_hidden_params")
    if not isinstance(hidden, dict):
        return {}
    headers: object = cast("dict[str, object]", hidden).get("additional_headers")
    return cast("dict[str, Any]", headers) if isinstance(headers, dict) else {}


def _content(body: dict[str, Any], started: float) -> str:
    choices: object = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _error(ModelErrorCategory.PROTOCOL, "router response has no choices", started)
    first: object = cast("list[object]", choices)[0]
    if not isinstance(first, dict):
        raise _error(ModelErrorCategory.PROTOCOL, "router choice is not an object", started)
    message: object = cast("dict[str, object]", first).get("message")
    if not isinstance(message, dict):
        raise _error(ModelErrorCategory.PROTOCOL, "router choice has no message", started)
    content: object = cast("dict[str, object]", message).get("content")
    if isinstance(content, str) and content.strip():
        return content
    raise _error(ModelErrorCategory.PROTOCOL, "router message content is empty", started)


def _delta(chunk: object) -> ModelDelta | None:
    """One streamed chunk as a delta, or ``None`` when it carries no text.

    Deployments disagree on where incremental text lives: the OpenAI-compatible
    ``choices[0].delta.content`` is universal, while a model's own deliberation
    arrives as ``reasoning_content`` on the families that expose it. Both are
    surfaced. A chunk with neither — the role-only opening chunk, or a trailing
    usage-only chunk with an empty ``choices`` — yields ``None``.
    """
    body = _as_mapping(chunk)
    choices: object = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first: object = cast("list[object]", choices)[0]
    if not isinstance(first, dict):
        return None
    delta: object = cast("dict[str, object]", first).get("delta")
    if not isinstance(delta, dict):
        return None
    fields = cast("dict[str, object]", delta)
    reasoning: object = fields.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return ModelDelta(text=reasoning, kind=ModelDeltaKind.REASONING)
    content: object = fields.get("content")
    if isinstance(content, str) and content:
        return ModelDelta(text=content, kind=ModelDeltaKind.CONTENT)
    return None


def _actual_model(body: dict[str, Any], alias: str) -> str:
    model: object = body.get("model")
    return model if isinstance(model, str) and model else alias


def _attempt(body: dict[str, Any]) -> int:
    retries = _router_headers(body).get("x-litellm-attempted-retries")
    if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0:
        return retries + 1
    return 1


def _fallback_reason(body: dict[str, Any], alias: str) -> str | None:
    """litellm counts the fallbacks it actually performed for this call."""
    headers = _router_headers(body)
    attempted = headers.get("x-litellm-attempted-fallbacks")
    if isinstance(attempted, int) and not isinstance(attempted, bool) and attempted > 0:
        return "fallback"
    group = headers.get("x-litellm-model-group")
    if isinstance(group, str) and group and group != alias:
        return "fallback"
    return None


def _usage(body: dict[str, Any]) -> ModelUsage:
    raw: object = body.get("usage")
    if not isinstance(raw, dict):
        return ModelUsage()
    usage = cast("dict[str, object]", raw)
    prompt_details: object = usage.get("prompt_tokens_details")
    completion_details: object = usage.get("completion_tokens_details")
    return ModelUsage(
        prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
        completion_tokens=_non_negative_int(usage.get("completion_tokens")),
        cached_tokens=_non_negative_int(
            cast("dict[str, object]", prompt_details).get("cached_tokens")
            if isinstance(prompt_details, dict)
            else None
        ),
        reasoning_tokens=_non_negative_int(
            cast("dict[str, object]", completion_details).get("reasoning_tokens")
            if isinstance(completion_details, dict)
            else None
        ),
    )


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _translate(exc: Exception, duration_ms: int) -> ModelGatewayError:
    """Map a litellm exception to a category without echoing its message.

    litellm exception text can embed the request body, so only the class name
    and a fixed category-level description cross this boundary.
    """
    if isinstance(exc, ModelGatewayError):
        return exc
    name = type(exc).__name__
    category = _ERROR_CATEGORIES.get(name)
    if category is None:
        category = ModelErrorCategory.UNAVAILABLE
        for known, mapped in _ERROR_CATEGORIES.items():
            if known in name:
                category = mapped
                break
    return ModelGatewayError(category, f"router call failed ({name})", duration_ms=duration_ms)


def _error(category: ModelErrorCategory, message: str, started: float) -> ModelGatewayError:
    return ModelGatewayError(category, message, duration_ms=_elapsed_ms(started))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


__all__ = ["LiteLlmRouterGateway", "silence_litellm_global_state"]
