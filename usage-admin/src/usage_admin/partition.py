"""Monthly partition maintenance for the raw event table.

The risk note in Story 8 is explicit: missed partition maintenance leads to
write failures. This admin command creates the partitions for a month range
ahead so ingest never fails because a partition is missing.
"""

from __future__ import annotations

from datetime import date

import psycopg


async def create_monthly_partitions(
    database_url: str,
    *,
    start_month: date | None = None,
    months_ahead: int = 3,
) -> list[str]:
    """Create (idempotently) partitions for ``months_ahead`` months from start."""
    current = start_month or date.today().replace(day=1)
    created: list[str] = []
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        for _ in range(max(1, months_ahead)):
            await connection.execute("SELECT usage_admin_create_partition(%s)", (current,))
            created.append(current.isoformat())
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
    return created


__all__ = ["create_monthly_partitions"]
