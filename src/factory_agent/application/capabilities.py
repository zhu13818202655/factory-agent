from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Capability(Protocol):
    @property
    def capability_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    capabilities: tuple[Capability, ...] = ()

    def get(self, capability_id: str) -> Capability | None:
        return next(
            (
                capability
                for capability in self.capabilities
                if capability.capability_id == capability_id
            ),
            None,
        )
