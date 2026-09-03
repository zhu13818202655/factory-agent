"""Periodic scope-deviation review task (Story 2).

Read-only analysis over the recorded consistency findings (exact/heuristic).
It aggregates them by role / capability / range dimension and renders a
redacted deviation report that an operator hands to the MES side for root
causing. It never writes back, never changes permissions, and never triggers
any MES call. A clean period (zero findings) produces an empty report — a
normal no-op run.

The report fields carry no sensitive values: no amounts and no plaintext work
numbers beyond the minimal non-sensitive set (counts + digests already stored
on each finding).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from factory_agent.ports.scope_violation import (
    ScopeViolationRecord,
    ScopeViolationStore,
    ViolationGroup,
)

#: Default review window: findings recorded in the trailing N days.
DEFAULT_WINDOW_DAYS = 7


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ScopeReviewReport:
    """Redacted deviation report for one review run."""

    generated_at: datetime
    window_start: datetime
    window_end: datetime
    groups: tuple[ViolationGroup, ...] = ()
    total_findings: int = 0

    @property
    def empty(self) -> bool:
        return self.total_findings == 0


@dataclass
class _Accumulator:
    count: int = 0
    row_count_total: int = 0
    latest_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc))


class ScopeReviewService:
    """Aggregates recorded scope findings into a redacted report."""

    def __init__(
        self,
        store: ScopeViolationStore,
        *,
        clock: Clock | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        self._store = store
        self._window_days = window_days
        self._clock = clock or _SystemClock()

    async def run_once(self, now: datetime | None = None) -> ScopeReviewReport:
        generated_at = now or self._clock.now()
        window_start = generated_at - timedelta(days=self._window_days)
        records = await self._store.list(window_start)
        if not records:
            return ScopeReviewReport(
                generated_at=generated_at,
                window_start=window_start,
                window_end=generated_at,
            )

        accumulators: dict[tuple[str, ...], _Accumulator] = {}
        for record in records:
            key = _group_key(record)
            accumulator = accumulators.setdefault(key, _Accumulator())
            accumulator.count += 1
            accumulator.row_count_total += record.row_count
            if record.created_at > accumulator.latest_at:
                accumulator.latest_at = record.created_at

        groups = tuple(
            ViolationGroup(
                tenant_id=key[0],
                role=key[1],
                capability_id=key[2],
                level=key[3],
                reason_code=key[4],
                count=accumulator.count,
                row_count_total=accumulator.row_count_total,
                latest_at=accumulator.latest_at,
            )
            for key, accumulator in sorted(
                accumulators.items(), key=lambda item: item[1].latest_at, reverse=True
            )
        )
        return ScopeReviewReport(
            generated_at=generated_at,
            window_start=window_start,
            window_end=generated_at,
            groups=groups,
            total_findings=len(records),
        )

    def render_text(self, report: ScopeReviewReport) -> str:
        """Human-readable redacted report (no sensitive values)."""
        lines = [
            "scope-review report",
            f"window: {report.window_start.isoformat()} -> {report.window_end.isoformat()}",
            f"findings: {report.total_findings}",
        ]
        if report.empty:
            lines.append("no scope deviations in window (normal no-op run)")
            return "\n".join(lines)
        lines.append("groups (tenant | role | capability | level | code | count | rows):")
        for group in report.groups:
            lines.append(
                "  "
                + " | ".join(
                    (
                        group.tenant_id,
                        group.role,
                        group.capability_id,
                        group.level,
                        group.reason_code,
                        str(group.count),
                        str(group.row_count_total),
                    )
                )
            )
        return "\n".join(lines)


def _group_key(record: ScopeViolationRecord) -> tuple[str, ...]:
    return (
        str(record.tenant_id),
        record.role.value,
        record.capability_id,
        record.level,
        record.reason_code,
    )


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


__all__ = ["DEFAULT_WINDOW_DAYS", "ScopeReviewReport", "ScopeReviewService"]
