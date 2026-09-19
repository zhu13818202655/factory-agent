"""Retention routines for the statistics surface.

The platform admin audit is retained for 180 days and then purged. Run it as a
one-shot command (``python -m factory_agent.cli retention``) from an external
scheduler — periodic maintenance in this repository is a cron-driven command,
not an in-process timer. Export artifacts are covered by the artifact
exporter's own retention window, so only the audit table is maintained here.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from factory_agent.statistics.store import UsageStore

AUDIT_RETENTION_DAYS = 180


@dataclass(frozen=True, slots=True)
class RetentionRun:
    purged_audit: int
    cutoff: datetime


class RetentionService:
    def __init__(
        self,
        store: UsageStore,
        *,
        clock: Callable[[], datetime],
        audit_retention_days: int = AUDIT_RETENTION_DAYS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit_retention_days = audit_retention_days

    async def run_once(self) -> RetentionRun:
        cutoff = self._clock() - timedelta(days=self._audit_retention_days)
        purged = await self._store.purge_audit_before(cutoff)
        return RetentionRun(purged_audit=purged, cutoff=cutoff)


__all__ = ["AUDIT_RETENTION_DAYS", "RetentionRun", "RetentionService"]
