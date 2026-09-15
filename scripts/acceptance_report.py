"""Acceptance readout for the link-optimization delivery (plan §4.1).

Reads the shared metering tables only — never writes, never touches business
tables. Prints, for one window: the per-event-type p50/p95 duration
distribution, the three-part duration identity for completed interactions, and
the LLM calls per interaction (the metric that should drop from 3 to 2 once the
scope guard is merged into EXTRACT, ADR-0008).

Pass two windows to get a before/after comparison side by side.

    python scripts/acceptance_report.py --since 2026-09-01T00:00
    python scripts/acceptance_report.py --before 2026-09-01T00:00 --after 2026-09-08T00:00

The database URL comes from ``--dsn`` or ``FACTORY_AGENT_POSTGRES_URL``.

The ``duration_ms`` values are text in JSONB, so a malformed value would abort
the whole aggregate; the queries therefore filter to digit-only values instead
of casting blindly. Everything else matches the delivery document's SQL.
"""

import argparse
import os
import sys
from datetime import datetime

import psycopg

_DURATION_IS_NUMERIC = "(payload->>'duration_ms') ~ '^[0-9]+$'"

_DISTRIBUTION_SQL = f"""
SELECT event_type,
       percentile_disc(0.5)  WITHIN GROUP (ORDER BY (payload->>'duration_ms')::int) AS p50,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY (payload->>'duration_ms')::int) AS p95,
       count(*) AS samples
FROM usage_event
WHERE event_type IN ('interaction_completed','llm_call_completed','mes_call_completed')
  AND occurred_at >= %s
  AND {_DURATION_IS_NUMERIC}
GROUP BY event_type
ORDER BY event_type
"""

_IDENTITY_SQL = """
SELECT count(*) AS rows_total,
       count(*) FILTER (WHERE (payload->>'llm_duration_ms') IS NOT NULL) AS with_llm,
       count(*) FILTER (
           WHERE (payload->>'llm_duration_ms') ~ '^[0-9]+$'
             AND (payload->>'mes_duration_ms') ~ '^[0-9]+$'
             AND (payload->>'duration_ms')     ~ '^[0-9]+$'
             AND (payload->>'llm_duration_ms')::int + (payload->>'mes_duration_ms')::int
                 <= (payload->>'duration_ms')::int
       ) AS identity_holds,
       max((payload->>'local_duration_ms')::int) FILTER (
           WHERE (payload->>'local_duration_ms') ~ '^[0-9]+$'
       ) AS max_local
FROM usage_event
WHERE event_type = 'interaction_completed' AND occurred_at >= %s
"""

_LLM_CALLS_SQL = """
SELECT calls, count(*) AS interactions
FROM (
    SELECT payload->>'interaction_id' AS interaction_id, count(*) AS calls
    FROM usage_event
    WHERE event_type = 'llm_call_completed' AND occurred_at >= %s
    GROUP BY 1
) per_interaction
WHERE interaction_id IS NOT NULL
GROUP BY calls
ORDER BY calls
"""


def _connect(dsn: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(dsn)


def _print_table(headers: list[str], rows: list[tuple[object, ...]]) -> None:
    if not rows:
        print("  (no rows)")
        return
    cells = [[str(value) for value in row] for row in rows]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in cells))
        for index in range(len(headers))
    ]
    print("  " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  " + "-+-".join("-" * width for width in widths))
    for row in cells:
        print("  " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def report(dsn: str, label: str, since: datetime) -> None:
    print(f"\n=== {label} (occurred_at >= {since.isoformat()}) ===")
    with _connect(dsn) as connection:
        distribution = connection.execute(_DISTRIBUTION_SQL, (since,)).fetchall()
        print("\n[p50/p95 per event type]")
        _print_table(["event_type", "p50", "p95", "samples"], distribution)

        identity = connection.execute(_IDENTITY_SQL, (since,)).fetchall()
        print("\n[three-part duration identity — llm + mes <= total]")
        _print_table(["rows", "with_llm", "identity_holds", "max_local"], identity)

        calls = connection.execute(_LLM_CALLS_SQL, (since,)).fetchall()
        print("\n[LLM calls per interaction — merged guard should move 3 -> 2]")
        _print_table(["calls", "interactions"], calls)


def _parse_moment(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("FACTORY_AGENT_POSTGRES_URL", ""))
    parser.add_argument("--since", help="single window start (ISO 8601)")
    parser.add_argument("--before", help="comparison window start (ISO 8601)")
    parser.add_argument("--after", help="comparison window start (ISO 8601); needs --before")
    args = parser.parse_args(argv)

    if not args.dsn:
        parser.error("no DSN: pass --dsn or set FACTORY_AGENT_POSTGRES_URL")
    if bool(args.before) != bool(args.after):
        parser.error("--before and --after must be given together")
    if not args.since and not args.before:
        parser.error("pass --since, or --before with --after")

    try:
        if args.since:
            report(args.dsn, "window", _parse_moment(args.since))
        else:
            report(args.dsn, "before", _parse_moment(args.before))
            report(args.dsn, "after", _parse_moment(args.after))
    except psycopg.Error as error:
        print(f"database error: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
