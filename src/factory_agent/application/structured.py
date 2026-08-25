"""Structured model output: extraction, validation, and one semantic repair.

ADR-0004 assigns schema validation and at most one semantic repair to the
application. Gateway transport failures stay ``ModelGatewayError``; a model that
answers but does not satisfy the schema raises ``StructuredOutputError``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, cast

from factory_agent.ports import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStage,
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

REPAIR_INSTRUCTION = (
    "Your previous reply was not a single valid JSON object matching the "
    "requested schema. Reply again with only the JSON object, no prose and no "
    "code fences."
)


class StructuredOutputError(Exception):
    """The gateway answered but the payload is not usable by the application."""

    def __init__(self, reason: str, *, attempts: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of one JSON object from raw, fenced, or inline text."""
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    for match in _JSON_FENCE.finditer(text):
        fenced = match.group(1).strip()
        if fenced:
            candidates.append(fenced)
    candidates.append(text.strip())
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])

    for candidate in candidates:
        try:
            value: object = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return cast("dict[str, Any]", value)
    return None


@dataclass(frozen=True, slots=True)
class StructuredResult:
    payload: dict[str, Any]
    response: ModelResponse
    attempts: int
    repaired: bool


async def request_structured_object(
    gateway: ModelGateway,
    request: ModelRequest,
    *,
    max_repair_attempts: int = 1,
) -> StructuredResult:
    """Run one logical call, repairing an unusable payload at most once."""
    attempt = 0
    response = await gateway.complete(request)
    attempt += 1
    payload = extract_json_object(response.content)
    if payload is not None:
        return StructuredResult(
            payload=payload, response=response, attempts=attempt, repaired=False
        )

    if max_repair_attempts < 1:
        raise StructuredOutputError("model output is not a JSON object", attempts=attempt)

    repair_request = replace(
        request,
        stage=ModelStage.REPAIR,
        json_output=True,
        messages=(
            *request.messages,
            ModelMessage(role="assistant", content=response.content),
            ModelMessage(role="user", content=REPAIR_INSTRUCTION),
        ),
    )
    repaired_response = await gateway.complete(repair_request)
    attempt += 1
    payload = extract_json_object(repaired_response.content)
    if payload is None:
        raise StructuredOutputError(
            "model output is not a JSON object after repair", attempts=attempt
        )
    return StructuredResult(
        payload=payload, response=repaired_response, attempts=attempt, repaired=True
    )


__all__ = [
    "REPAIR_INSTRUCTION",
    "StructuredOutputError",
    "StructuredResult",
    "extract_json_object",
    "request_structured_object",
]
