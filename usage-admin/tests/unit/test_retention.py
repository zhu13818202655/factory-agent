"""Retention routine: 180-day admin audit purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from usage_admin.retention import RetentionService
from usage_admin.store import AuditEntry, InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


def audit(entry_id: str, created_at: datetime) -> AuditEntry:
    return AuditEntry(
        audit_id=entry_id,
        principal_id="ops-1",
        action="export.create",
        target=None,
        detail={},
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_audit_older_than_180_days_is_purged() -> None:
    store = InMemoryUsageStore()
    await store.record_audit(audit("old", NOW - timedelta(days=200)))
    await store.record_audit(audit("recent", NOW - timedelta(days=10)))
    service = RetentionService(store, clock=lambda: NOW)

    run = await service.run_once()

    assert run.purged_audit == 1
    assert [entry.audit_id for entry in store.audits] == ["recent"]


@pytest.mark.asyncio
async def test_nothing_is_purged_when_all_audit_is_recent() -> None:
    store = InMemoryUsageStore()
    await store.record_audit(audit("recent", NOW - timedelta(days=30)))
    service = RetentionService(store, clock=lambda: NOW)

    run = await service.run_once()

    assert run.purged_audit == 0
    assert len(store.audits) == 1
