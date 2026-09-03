"""Role-consistency violation records (Story 2 review table).

Structured rows for exact and heuristic consistency findings. They are the
independent review surface the periodic scope-review task aggregates; they
never carry sensitive values — only counts, digests of the offending values,
and the human-readable expected/actual range summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from factory_agent.domain import Role, TenantId, UserId


@dataclass(frozen=True, slots=True)
class ScopeViolationRecord:
    violation_id: str
    tenant_id: TenantId
    user_id: UserId
    role: Role
    capability_id: str
    level: str  # exact_hit | heuristic_hit
    mode: str  # strict | production
    reason_code: str
    interaction_id: str | None
    expected_range: str
    actual_summary: str
    row_count: int
    sample_count: int
    sample_digests: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ViolationGroup:
    """Aggregated deviation group used by the periodic review report."""

    tenant_id: str
    role: str
    capability_id: str
    level: str
    reason_code: str
    count: int
    row_count_total: int
    latest_at: datetime


class ScopeViolationStore(Protocol):
    """Durable review surface for role-consistency findings.

    Implementations must never raise into the session pipeline; callers treat
    recording as best-effort (alerts happen through logs and audit events).
    """

    async def record(self, entry: ScopeViolationRecord) -> None: ...

    async def list(
        self,
        since: datetime,
        limit: int = 1000,
    ) -> tuple[ScopeViolationRecord, ...]:
        """List violations recorded since a cutoff (oldest first)."""
        ...


__all__ = [
    "ScopeViolationRecord",
    "ScopeViolationStore",
    "ViolationGroup",
]
