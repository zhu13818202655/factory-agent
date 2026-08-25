"""Typed model gateway contract.

The product always talks to exactly one LiteLLM OpenAI-compatible gateway using
logical aliases. Provider URLs, provider credentials, network retries, and
provider fallback chains belong to LiteLLM and never appear in this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

ModelRole = Literal["system", "user", "assistant"]


class ModelStage(StrEnum):
    """Bounded set of prompt stages the product is allowed to run."""

    CLASSIFY = "classify"
    EXTRACT = "extract"
    CLARIFY = "clarify"
    SUMMARIZE = "summarize"
    REPAIR = "repair"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: ModelRole
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One logical gateway call. ``logical_call_id`` groups physical attempts."""

    model_alias: str
    messages: tuple[ModelMessage, ...]
    stage: ModelStage
    logical_call_id: str
    json_output: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    actual_model: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    duration_ms: int = 0
    attempt: int = 1
    fallback_reason: str | None = None


class ModelErrorCategory(StrEnum):
    """Gateway-side failure categories; distinct from semantic failures."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    UNAUTHENTICATED = "unauthenticated"
    PROTOCOL = "protocol"
    NOT_CONFIGURED = "not_configured"


class ModelGatewayError(Exception):
    """Transport or protocol failure reported by the gateway.

    Messages are category-level only: prompts, completions, credentials, and
    raw provider payloads must never be attached.
    """

    def __init__(
        self,
        category: ModelErrorCategory,
        message: str,
        *,
        duration_ms: int = 0,
        attempt: int = 1,
    ) -> None:
        super().__init__(f"{category.value}: {message}")
        self.category = category
        self.message = message
        self.duration_ms = duration_ms
        self.attempt = attempt


class ModelGateway(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


__all__ = [
    "ModelErrorCategory",
    "ModelGateway",
    "ModelGatewayError",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "ModelStage",
    "ModelUsage",
]
