"""``usage_event`` partition maintenance (D-9).

``usage_event`` is range-partitioned by month, so a write can only land in a
child table that already exists. Relying on a migration to seed the calendar
means the first write of an unseeded month fails — and because metering
failures are isolated (alerted, never rolled back into the answer), that
failure is silent data loss rather than a visible error.

This maintainer keeps the current and the following month present at all times,
so crossing a month boundary needs no restart and no operator. It is
deliberately fail-open: a maintenance failure must never take the service down
or block a question, but it must never be silent either — every failure logs
and alerts with the exact month and partition name an operator needs to repair
by hand::

    SELECT factory_agent_create_partition(DATE '2026-11-01');
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import psycopg

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.statistics.alerts import AlertSink, LoggingAlertSink

_LOGGER = get_logger("factory_agent.statistics.partitions")

#: PostgreSQL SQLSTATE for ``duplicate_table``: two workers or a restart racing
#: the same ``CREATE TABLE IF NOT EXISTS`` is expected and harmless.
_DUPLICATE_TABLE = "42P07"


@dataclass(frozen=True, slots=True)
class PartitionTarget:
    """One monthly child table of ``usage_event``."""

    month: date
    name: str


@dataclass(frozen=True, slots=True)
class PartitionRun:
    """Outcome of one maintenance pass."""

    ensured: tuple[PartitionTarget, ...]
    failed: tuple[PartitionTarget, ...]


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def partition_name(month: date) -> str:
    return f"usage_event_{month.strftime('%Y%m')}"


class UsagePartitionMaintainer:
    """Ensures the current and next ``usage_event`` partitions exist."""

    def __init__(
        self,
        database_url: str,
        *,
        statement_timeout_ms: int | None = None,
        alerts: AlertSink | None = None,
        clock: Callable[[], date] | None = None,
    ) -> None:
        self._database_url = database_url
        self._statement_timeout_ms = statement_timeout_ms
        self._alerts = alerts if alerts is not None else LoggingAlertSink()
        self._clock = clock or (lambda: datetime.now(timezone.utc).date())

    def targets(self, reference_month: date | None = None) -> tuple[PartitionTarget, ...]:
        """The two months that must always exist: this one and the next."""
        current = _month_start(reference_month or self._clock())
        return (
            PartitionTarget(month=current, name=partition_name(current)),
            PartitionTarget(month=_next_month(current), name=partition_name(_next_month(current))),
        )

    async def ensure(self, reference_month: date | None = None) -> PartitionRun:
        """Create any missing partition; never raise, always report."""
        targets = self.targets(reference_month)
        ensured: list[PartitionTarget] = []
        failed: list[PartitionTarget] = []
        # ``autocommit`` on purpose: each target is a single DDL statement, and a
        # plain connection would roll the created partitions back on close — the
        # DDL would appear to succeed while no partition exists.
        connect_options: dict[str, Any] = {"autocommit": True}
        if self._statement_timeout_ms is not None:
            # Only set when asked for. Passing an empty ``options`` would replace
            # (not extend) whatever the DSN already carries, silently dropping a
            # caller-supplied ``default_transaction_read_only`` and turning a
            # deliberately read-only connection into a writable one.
            connect_options["options"] = f"-c statement_timeout={int(self._statement_timeout_ms)}"
        try:
            connection = await psycopg.AsyncConnection.connect(
                self._database_url, **connect_options
            )
        except Exception:  # noqa: BLE001 - maintenance must never block startup
            _LOGGER.exception("usage.partition.connect_failed")
            for target in targets:
                failed.append(target)
            await self._alert_failure(targets, "connect_failed")
            return PartitionRun(ensured=(), failed=tuple(failed))

        try:
            for target in targets:
                if await self._create(connection, target):
                    ensured.append(target)
                else:
                    failed.append(target)
        except Exception:  # noqa: BLE001 - maintenance never raises into the caller
            _LOGGER.exception("usage.partition.ensure_failed")
            failed = [target for target in targets if target not in ensured]
        finally:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 - a failed close is not the caller's problem
                _LOGGER.exception("usage.partition.close_failed")
        return PartitionRun(ensured=tuple(ensured), failed=tuple(failed))

    async def ensure_forever(self, interval_seconds: float) -> None:
        """Run ``ensure`` every ``interval_seconds`` until cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.ensure()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sweep failure never kills the loop
                _LOGGER.exception("usage.partition.sweep_failed")

    async def _create(
        self, connection: psycopg.AsyncConnection[Any], target: PartitionTarget
    ) -> bool:
        # The month travels as a bound date parameter, never interpolated.
        statement = "SELECT factory_agent_create_partition(%s)"
        try:
            await connection.execute(statement, (target.month,))
        except psycopg.errors.DuplicateTable:
            # Another worker created it between our check and our DDL.
            return True
        except psycopg.Error as exc:
            if exc.sqlstate == _DUPLICATE_TABLE:
                return True
            _LOGGER.exception(
                "usage.partition.create_failed month={} partition={}",
                target.month.isoformat(),
                target.name,
            )
            await self._alert_failure((target,), "create_failed")
            return False
        return True

    async def _alert_failure(self, targets: tuple[PartitionTarget, ...], reason: str) -> None:
        for target in targets:
            # Metadata only: a month and a table name are not sensitive, and
            # they are exactly what an operator needs to repair the gap.
            try:
                await self._alerts.alert(
                    "usage.partition.ensure_failed",
                    {
                        "month": target.month.isoformat(),
                        "partition": target.name,
                        "reason": reason,
                    },
                )
            except Exception:  # noqa: BLE001 - alerting is best effort
                _LOGGER.exception("usage.partition.alert_failed")


__all__ = [
    "PartitionRun",
    "PartitionTarget",
    "UsagePartitionMaintainer",
    "partition_name",
]
