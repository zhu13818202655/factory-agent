"""``usage_event`` partition maintenance without a database.

The maintainer's contract is "never raise, always report": a missing partition is
silent data loss, so a failure has to be loud, and an outage has to be harmless.
"""

from datetime import date

import pytest

from factory_agent.statistics.partitions import UsagePartitionMaintainer, partition_name


class RecordingAlerts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def alert(self, kind: str, detail: dict[str, object]) -> None:
        self.calls.append((kind, detail))


class ExplodingAlerts:
    async def alert(self, kind: str, detail: dict[str, object]) -> None:
        raise RuntimeError("alert sink is down")


def maintainer(alerts: object | None = None, *, url: str = "postgresql://invalid/db"):
    return UsagePartitionMaintainer(
        url,
        statement_timeout_ms=30_000,
        alerts=alerts,  # type: ignore[arg-type]
        clock=lambda: date(2026, 11, 5),
    )


def test_targets_are_the_reference_month_and_the_next() -> None:
    run = maintainer().targets(date(2026, 11, 1))
    assert [target.name for target in run] == ["usage_event_202611", "usage_event_202612"]


def test_targets_cross_the_year_boundary() -> None:
    run = maintainer().targets(date(2026, 12, 31))
    assert [target.name for target in run] == ["usage_event_202612", "usage_event_202701"]


def test_targets_default_to_the_injected_clock() -> None:
    assert [target.name for target in maintainer().targets()] == [
        "usage_event_202611",
        "usage_event_202612",
    ]


def test_partition_name_is_the_month() -> None:
    assert partition_name(date(2026, 1, 1)) == "usage_event_202601"


@pytest.mark.asyncio
async def test_connect_failure_reports_both_months_and_alerts() -> None:
    """An unreachable database must not raise, and must not be quiet."""
    alerts = RecordingAlerts()
    # Port 1 is not listening, so the connection attempt fails immediately.
    run = await maintainer(alerts, url="postgresql://127.0.0.1:1/none").ensure(date(2026, 11, 1))

    assert run.ensured == ()
    assert [target.name for target in run.failed] == ["usage_event_202611", "usage_event_202612"]
    assert [kind for kind, _ in alerts.calls] == ["usage.partition.ensure_failed"] * 2
    months = [detail["month"] for _, detail in alerts.calls]
    assert months == ["2026-11-01", "2026-12-01"]


@pytest.mark.asyncio
async def test_a_broken_alert_sink_does_not_break_maintenance() -> None:
    run = await maintainer(ExplodingAlerts(), url="postgresql://127.0.0.1:1/none").ensure(
        date(2026, 11, 1)
    )

    assert [target.name for target in run.failed] == ["usage_event_202611", "usage_event_202612"]
