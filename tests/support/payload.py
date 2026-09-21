"""Narrowing helpers for asserting on JSON-shaped payloads.

Capability results and card payloads are typed ``dict[str, object]`` because that
is what the wire format is, so a nested assertion (``card["table"]["rows"]``)
would index ``object``. These helpers narrow one level with a runtime
``isinstance`` check, which keeps the assertion honest: a payload that stops
being an object or an array fails at the narrowing point instead of silently
typing as ``object`` at the index.
"""

from typing import Any, cast


def as_dict(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def as_list(value: object) -> list[Any]:
    assert isinstance(value, list)
    return cast("list[Any]", value)
