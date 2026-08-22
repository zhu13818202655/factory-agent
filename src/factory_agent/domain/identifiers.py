"""Stable identifiers shared across application boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TenantId:
    """Stable identifier for one tenant."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value != self.value.strip():
            raise ValueError("tenant ID must be non-empty and have no surrounding whitespace")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class InteractionId:
    """Stable identifier for one user interaction."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value != self.value.strip():
            raise ValueError("interaction ID must be non-empty and have no surrounding whitespace")

    def __str__(self) -> str:
        return self.value
