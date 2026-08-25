"""SSE framing with a durable event id and strict JSON serialization.

``json.dumps(allow_nan=False)`` guarantees no ``NaN``/``Infinity`` token ever
reaches a browser; a payload that still contains a non-finite float is sanitized
to ``null`` instead of breaking the stream.
"""

from __future__ import annotations

import json
import math
from typing import Any

from factory_agent.domain import SessionEvent


def sanitize(value: Any) -> Any:
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def encode_event(event: SessionEvent) -> str:
    try:
        data = json.dumps(event.data, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        data = json.dumps(sanitize(event.data), ensure_ascii=False, default=str)
    return f"id: {event.sequence}\nevent: {event.name}\ndata: {data}\n\n"


def parse_last_event_id(raw: str | None) -> int:
    """A missing or malformed ``Last-Event-ID`` restarts from the beginning."""
    if raw is None:
        return 0
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return 0


__all__ = ["encode_event", "parse_last_event_id", "sanitize"]
