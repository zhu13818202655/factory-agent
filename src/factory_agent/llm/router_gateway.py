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

from __future__ import annotations

import time
from typing import Any, cast

import litellm
from litellm.router import Router

from factory_agent.llm.registry import ModelRegistry
from factory_agent.ports.model import (
    ModelErrorCategory,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)

_JSON_OBJECT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}

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
        router: Router | None = None,
    ) -> None:
        silence_litellm_global_state()
        self._registry = registry
        self._default_timeout_seconds = default_timeout_seconds
        self._default_temperature = default_temperature
        self._default_top_p = default_top_p
        self._default_max_output_tokens = default_max_output_tokens
        self._router = router or Router(
            model_list=_model_list(registry),
            fallbacks=_fallbacks(registry),
            num_retries=num_retries,
            allowed_fails=allowed_fails,
            cooldown_time=cooldown_seconds,
            timeout=default_timeout_seconds,
            set_verbose=False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ModelGatewayError(ModelErrorCategory.PROTOCOL, "messages cannot be empty")
        if request.model_alias not in self._registry.aliases():
            raise ModelGatewayError(
                ModelErrorCategory.NOT_CONFIGURED,
                f"alias {request.model_alias} has no configured deployment",
            )

        started = time.monotonic()
        try:
            raw = await self._acompletion(request)
        except Exception as exc:
            raise _translate(exc, _elapsed_ms(started)) from exc

        body = _as_mapping(raw)
        return ModelResponse(
            content=_content(body, started),
            actual_model=_actual_model(body, request.model_alias),
            usage=_usage(body),
            duration_ms=_elapsed_ms(started),
            attempt=_attempt(body),
            fallback_reason=_fallback_reason(body, request.model_alias),
        )

    async def _acompletion(self, request: ModelRequest) -> object:
        """Sole litellm call site; its loose typing is contained here."""
        call = cast("Any", self._router.acompletion)  # pyright: ignore[reportUnknownMemberType]
        return await call(
            model=request.model_alias,
            messages=_messages(request),
            **self._call_options(request),
        )

    def _call_options(self, request: ModelRequest) -> dict[str, Any]:
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
        return options


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
