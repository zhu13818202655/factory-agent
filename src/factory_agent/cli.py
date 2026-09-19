"""Operator CLI entry points (external cron friendly).

Periodic maintenance in this repository is expressed as one-shot commands
driven by an external scheduler rather than in-process timers. Three one-shots
live here:

* ``scope-review`` runs the read-only role-consistency deviation review once and
  prints a redacted report;
* ``retention`` purges platform audit rows older than the retention window;
* ``rollup`` recomputes the usage rollups for a window, which is what every
  reported KPI reads (backfill after downtime, or a wide replay after a metric
  version bump).

``usage_event`` partition maintenance and the routine rollup sweep are
deliberately *not* here: both have to keep up with writes rather than with a
cron cadence, so they run in the API process's lifespan. ``rollup`` exists for
the cases the sweep cannot cover — an instance that was down longer than the
sweep window, or an operator widening the window on purpose.

All commands are reachable as ``python -m factory_agent.cli <name>`` and as
their own console scripts.
"""

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Sequence

from factory_agent.application.rollup import RollupEngine, RollupWorker
from factory_agent.application.scope_review import ScopeReviewService
from factory_agent.config import get_settings
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.rollup_store import SqlRollupStore
from factory_agent.persistence.scope_violation import SqlScopeViolationStore
from factory_agent.statistics.config import StatisticsSettings
from factory_agent.statistics.retention import AUDIT_RETENTION_DAYS, RetentionService
from factory_agent.statistics.store import PostgresUsageStore


def _require_postgres_url() -> str:
    settings = get_settings()
    if settings.postgres_url is None:
        raise SystemExit("FACTORY_AGENT_POSTGRES_URL is required")
    return str(settings.postgres_url)


def scope_review_main(argv: Sequence[str] | None = None) -> None:
    """Run the scope-deviation review once and print the redacted report."""
    parser = argparse.ArgumentParser(description="Run the role-consistency deviation review")
    parser.add_argument("--window-days", type=int, default=7, help="review window in days")
    args = parser.parse_args(argv)

    engine = create_session_engine(_require_postgres_url())
    service = ScopeReviewService(
        SqlScopeViolationStore(engine),
        window_days=args.window_days,
    )

    async def _run() -> str:
        report = await service.run_once()
        return service.render_text(report)

    print(asyncio.run(_run()))


def retention_main(argv: Sequence[str] | None = None) -> None:
    """Purge platform audit rows older than the retention window."""
    parser = argparse.ArgumentParser(
        description="Purge platform audit rows past the retention window"
    )
    parser.add_argument(
        "--audit-retention-days",
        type=int,
        default=AUDIT_RETENTION_DAYS,
        help="override the default audit retention window",
    )
    args = parser.parse_args(argv)

    statistics_settings = StatisticsSettings()
    service = RetentionService(
        PostgresUsageStore(
            _require_postgres_url(),
            statement_timeout_ms=statistics_settings.statement_timeout_ms,
        ),
        clock=lambda: datetime.now(timezone.utc),
        audit_retention_days=args.audit_retention_days,
    )
    run = asyncio.run(service.run_once())
    print(f"audit retention: purged {run.purged_audit} rows older than {run.cutoff.isoformat()}")


def rollup_main(argv: Sequence[str] | None = None) -> None:
    """Recompute the usage rollups for the trailing window, once."""
    parser = argparse.ArgumentParser(
        description="Recompute usage rollups for a window (no-op on a quiet factory)"
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="window to recompute, in hours (default: the configured sweep window)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    window_hours = args.hours if args.hours is not None else settings.usage_rollup_window_hours
    if window_hours <= 0:
        raise SystemExit("--hours must be positive")
    engine = RollupEngine(
        SqlRollupStore(create_session_engine(_require_postgres_url())),
        clock=lambda: datetime.now(timezone.utc),
    )
    worker = RollupWorker(engine, window_hours=window_hours)
    run = asyncio.run(worker.run_once(datetime.now(timezone.utc)))
    print(
        f"rollup: {run.start:%Y-%m-%dT%H:%M:%SZ} -> {run.end:%Y-%m-%dT%H:%M:%SZ}"
        f" tenants={len(run.tenant_ids)} hourly_buckets={run.hourly_rows}"
        f" daily_buckets={run.daily_rows}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch ``python -m factory_agent.cli <command>``."""
    parser = argparse.ArgumentParser(description="factory-agent operator commands")
    parser.add_argument("command", choices=("scope-review", "retention", "rollup"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    if parsed.command == "scope-review":
        scope_review_main(parsed.args)
    elif parsed.command == "retention":
        retention_main(parsed.args)
    else:
        rollup_main(parsed.args)


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    main()


__all__ = ["main", "retention_main", "rollup_main", "scope_review_main"]
