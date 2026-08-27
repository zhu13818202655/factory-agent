"""Admin command entrypoints for the rollup worker, partition maintenance, and
retention routines."""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from typing import Sequence

from usage_admin.config import get_settings
from usage_admin.partition import create_monthly_partitions
from usage_admin.retention import RetentionService
from usage_admin.rollup import AdvisoryLock, RollupEngine, RollupWorker
from usage_admin.store import PostgresUsageStore


def _require_database() -> str:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("USAGE_ADMIN_DATABASE_URL is required")
    return settings.database_url.get_secret_value()


async def _rollup_forever(tenant_ids: frozenset[str], poll_seconds: float) -> None:
    database_url = _require_database()
    engine = RollupEngine(
        PostgresUsageStore(database_url),
        clock=lambda: datetime.now(timezone.utc),
    )
    worker = RollupWorker(engine, tenant_ids=tenant_ids, poll_seconds=poll_seconds)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    async with AdvisoryLock(database_url):
        await worker.run_forever(stop)


def rollup_main(argv: Sequence[str] | None = None) -> None:
    """Run the rollup worker under a PostgreSQL advisory lock."""
    import argparse

    parser = argparse.ArgumentParser(description="Run the usage-admin rollup worker")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--tenant", action="append", default=[], help="restrict to tenants")
    args = parser.parse_args(argv)
    tenant_ids = frozenset(args.tenant)
    asyncio.run(_rollup_forever(tenant_ids, args.poll_seconds))


def partition_main(argv: Sequence[str] | None = None) -> None:
    """Create monthly partitions ahead of the current month."""
    import argparse

    parser = argparse.ArgumentParser(description="Create usage_event monthly partitions")
    parser.add_argument("--months-ahead", type=int, default=3)
    args = parser.parse_args(argv)
    database_url = _require_database()
    created = asyncio.run(create_monthly_partitions(database_url, months_ahead=args.months_ahead))
    print(f"partitions ensured for: {', '.join(created)}")


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


__all__ = ["partition_main", "retention_main", "rollup_main"]
