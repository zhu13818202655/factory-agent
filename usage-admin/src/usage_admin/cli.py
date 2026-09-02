"""Admin command entrypoints for retention maintenance.

factory-agent writes the metering events and rollup rows directly; this
service only owns ``tenant_registry`` / ``platform_principal`` / ``admin_audit``
(and the export record table ``usage_export``), so its admin commands cover
audit retention only. Partition maintenance for ``usage_event`` is
factory-agent's responsibility.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Sequence

from usage_admin.config import get_settings
from usage_admin.retention import RetentionService
from usage_admin.store import PostgresUsageStore


def _require_database() -> str:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("USAGE_ADMIN_DATABASE_URL is required")
    return settings.database_url.get_secret_value()


async def _run_retention() -> int:
    database_url = _require_database()
    service = RetentionService(
        PostgresUsageStore(database_url),
        clock=lambda: datetime.now(timezone.utc),
    )
    run = await service.run_once()
    print(f"audit retention: purged {run.purged_audit} rows older than {run.cutoff.isoformat()}")
    return run.purged_audit


def retention_main(argv: Sequence[str] | None = None) -> None:
    """Purge admin audit rows older than the 180-day retention window."""
    del argv
    asyncio.run(_run_retention())


__all__ = ["retention_main"]
