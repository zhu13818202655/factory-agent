from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from factory_agent.application.time_expressions import (
    TimeExpressionError,
    resolve_time_expression,
)

TZ = "Asia/Shanghai"
# 2026-08-24 06:00Z is 2026-08-24 14:00 in Asia/Shanghai (a Monday).
NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


def iso(expression: str) -> tuple[str, str]:
    resolved = resolve_time_expression(expression, NOW, TZ)
    return resolved.start.isoformat(), resolved.end.isoformat()


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("今天", ("2026-08-23T16:00:00+00:00", "2026-08-24T16:00:00+00:00")),
        ("昨天", ("2026-08-22T16:00:00+00:00", "2026-08-23T16:00:00+00:00")),
        ("本周", ("2026-08-23T16:00:00+00:00", "2026-08-30T16:00:00+00:00")),
        ("上周", ("2026-08-16T16:00:00+00:00", "2026-08-23T16:00:00+00:00")),
        ("本月", ("2026-07-31T16:00:00+00:00", "2026-08-31T16:00:00+00:00")),
        ("上个月", ("2026-06-30T16:00:00+00:00", "2026-07-31T16:00:00+00:00")),
        ("今年", ("2025-12-31T16:00:00+00:00", "2026-12-31T16:00:00+00:00")),
        ("2026-08", ("2026-07-31T16:00:00+00:00", "2026-08-31T16:00:00+00:00")),
        ("2026年7月", ("2026-06-30T16:00:00+00:00", "2026-07-31T16:00:00+00:00")),
        ("2026-08-01", ("2026-07-31T16:00:00+00:00", "2026-08-01T16:00:00+00:00")),
        ("近7天", ("2026-08-17T16:00:00+00:00", "2026-08-24T16:00:00+00:00")),
    ],
)
def test_relative_expressions_are_deterministic_in_the_factory_timezone(
    expression: str, expected: tuple[str, str]
) -> None:
    assert iso(expression) == expected


def test_quarter_boundaries_use_the_factory_calendar() -> None:
    start, end = iso("本季度")

    assert start == "2026-06-30T16:00:00+00:00"
    assert end == "2026-09-30T16:00:00+00:00"


def test_january_month_rollover_uses_the_previous_year() -> None:
    january = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)

    resolved = resolve_time_expression("上个月", january, TZ)

    assert resolved.start.isoformat() == "2025-11-30T16:00:00+00:00"
    assert resolved.end.isoformat() == "2025-12-31T16:00:00+00:00"


def test_ranges_are_half_open() -> None:
    today = resolve_time_expression("今天", NOW, TZ)
    tomorrow = resolve_time_expression("今天", NOW + timedelta(days=1), TZ)

    assert today.end == tomorrow.start


@pytest.mark.parametrize("expression", ["", "   ", "下辈子", "2026-13", "2026-02-30", "近0天"])
def test_unreviewed_expressions_are_rejected(expression: str) -> None:
    with pytest.raises(TimeExpressionError):
        resolve_time_expression(expression, NOW, TZ)


def test_the_same_expression_resolves_identically_for_the_same_clock() -> None:
    assert iso("上个月") == iso("上个月")
