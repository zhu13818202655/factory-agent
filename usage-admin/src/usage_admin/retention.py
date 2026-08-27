"""Retention routines for usage-admin.

The platform admin audit is retained for 180 days and then purged as a routine
task. Export artifacts are covered by the main service's 3-month cleanup; here
we only maintain the audit log that usage-admin owns.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from usage_admin.store import UsageStore

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
