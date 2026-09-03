"""In-memory scope-violation store for tests (Story 2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from factory_agent.ports.scope_violation import ScopeViolationRecord


@dataclass
class InMemoryScopeViolationStore:
    records: list[ScopeViolationRecord] = field(default_factory=list[ScopeViolationRecord])

    async def record(self, entry: ScopeViolationRecord) -> None:
        self.records = [
            existing for existing in self.records if existing.violation_id != entry.violation_id
        ]
        self.records.append(entry)

    async def list(
        self,
        since: datetime,
        limit: int = 1000,
    ) -> tuple[ScopeViolationRecord, ...]:
        ordered = sorted(
            (entry for entry in self.records if entry.created_at >= since),
            key=lambda entry: entry.created_at,
        )
        return tuple(ordered[:limit])
