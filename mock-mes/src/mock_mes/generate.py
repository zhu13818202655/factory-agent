"""mock-mes-generate CLI (Story 10).

Deterministic production-like data generation into PostgreSQL. Manual runs and
scheduled runs share this entry point; the batch ledger makes every run
idempotent (``(seed, day)`` is generated at most once). Scale comes from the
``MOCK_MES_HEADCOUNT`` / ``MOCK_MES_DEPARTMENTS`` / … settings.

Examples::

    mock-mes-generate --fill-missing
    mock-mes-generate --day 2026-08-28 --days 1
    mock-mes-generate --start 2026-08-01 --end 2026-08-31
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Sequence

import psycopg
import psycopg.rows

from mock_mes.config import MockMesSettings, get_settings
from mock_mes.generator.engine import fill_window, generate_day, window_digest


async def _run(
    *,
    settings: MockMesSettings,
    database_url: str,
    day: date | None,
    days: int,
    start: date | None,
    end: date | None,
) -> int:
    run_id = f"cli-{datetime.now().isoformat(timespec='seconds')}-{uuid.uuid4().hex[:6]}"
    async with await psycopg.AsyncConnection.connect(
        database_url,
        row_factory=psycopg.rows.dict_row,  # type: ignore[arg-type]
    ) as connection:
        if day is not None:
            batch = await generate_day(connection, settings, day, run_id)
            print(f"{batch.day} {batch.status} rows={batch.row_count} hash={batch.data_hash}")
            return 0
        if start is None or end is None:
            settings = get_settings()
            start = start or settings.resolved_data_start
            end = end or settings.resolved_data_end
        if days:
            start = start or date.today() - timedelta(days=days - 1)
            end = end or date.today()
        report = await fill_window(connection, settings, start, end, run_id)
        digest = await window_digest(connection, settings, report.window_start, report.window_end)
        print(
            f"window={report.window_start}..{report.window_end} "
            f"generated={report.generated} skipped={report.skipped} "
            f"window_hash={digest}"
        )
        return 0


def main(argv: Sequence[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Generate production-like Mock MES data into PG")
    parser.add_argument("--seed", type=int, default=settings.seed)
    parser.add_argument("--database-url", default=os.environ.get("MOCK_MES_DATABASE_URL"))
    parser.add_argument("--day", type=date.fromisoformat, help="generate exactly this day")
    parser.add_argument("--days", type=int, default=0, help="backfill the last N days up to today")
    parser.add_argument(
        "--start", type=date.fromisoformat, help="window start (default: last Jan 1)"
    )
    parser.add_argument(
        "--end", type=date.fromisoformat, help="window end (default: virtual_now/today)"
    )
    parser.add_argument("--fill-missing", action="store_true", help="scan the window and fill gaps")
    args = parser.parse_args(argv)

    database_url = args.database_url or (
        settings.database_url.get_secret_value() if settings.database_url else None
    )
    if not database_url:
        parser.error("MOCK_MES_DATABASE_URL is required for generation")

    if args.fill_missing:
        asyncio.run(
            _run(
                settings=settings,
                database_url=database_url,
                day=None,
                days=0,
                start=args.start,
                end=args.end,
            )
        )
    else:
        asyncio.run(
            _run(
                settings=settings,
                database_url=database_url,
                day=args.day,
                days=args.days,
                start=args.start,
                end=args.end,
            )
        )


if __name__ == "__main__":
    main()
