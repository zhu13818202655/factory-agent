"""Deterministic factory-timezone resolution of relative time expressions.

The model may propose a time phrase, but the half-open ``TimeRange`` is always
computed here from an injected clock and the configured factory timezone, so
relative-time behaviour is reproducible in tests.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from factory_agent.domain import TimeRange

_RECENT_DAYS = re.compile(r"^(?:近|最近|过去)\s*(\d{1,3})\s*天$")
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_CN_MONTH = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月$")


class TimeExpressionError(ValueError):
    """Raised when a phrase is not in the reviewed relative-time vocabulary."""


def resolve_time_expression(expression: str, now: datetime, tz_name: str) -> TimeRange:
    """Resolve one reviewed phrase into a half-open UTC ``TimeRange``."""
    phrase = expression.strip()
    if not phrase:
        raise TimeExpressionError("time expression is empty")

    zone = ZoneInfo(tz_name)
    today = now.astimezone(zone).date()

    bounds = _resolve_named(phrase, today) or _resolve_pattern(phrase, today)
    if bounds is None:
        raise TimeExpressionError("time expression is not in the reviewed vocabulary")
    start_day, end_day = bounds
    return TimeRange(start=_to_utc(start_day, zone), end=_to_utc(end_day, zone))


def _resolve_named(phrase: str, today: date) -> tuple[date, date] | None:
    if phrase in ("今天", "今日"):
        return today, today + timedelta(days=1)
    if phrase in ("昨天", "昨日"):
        return today - timedelta(days=1), today
    if phrase == "前天":
        return today - timedelta(days=2), today - timedelta(days=1)
    if phrase in ("本周", "这周", "这个星期"):
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if phrase in ("上周", "上个星期"):
        this_week = today - timedelta(days=today.weekday())
        return this_week - timedelta(days=7), this_week
    if phrase in ("本月", "这个月", "当月"):
        start = today.replace(day=1)
        return start, _add_month(start)
    if phrase in ("上月", "上个月"):
        this_month = today.replace(day=1)
        return _subtract_month(this_month), this_month
    if phrase in ("本季度", "这个季度"):
        start = _quarter_start(today)
        return start, _add_month(_add_month(_add_month(start)))
    if phrase in ("上季度", "上个季度"):
        this_quarter = _quarter_start(today)
        return _subtract_month(_subtract_month(_subtract_month(this_quarter))), this_quarter
    if phrase in ("今年", "本年"):
        start = today.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1)
    if phrase == "去年":
        this_year = today.replace(month=1, day=1)
        return this_year.replace(year=this_year.year - 1), this_year
    return None


def _resolve_pattern(phrase: str, today: date) -> tuple[date, date] | None:
    recent = _RECENT_DAYS.match(phrase)
    if recent:
        days = int(recent.group(1))
        if days < 1:
            raise TimeExpressionError("relative day count must be positive")
        end = today + timedelta(days=1)
        return end - timedelta(days=days), end

    iso_day = _ISO_DAY.match(phrase)
    if iso_day:
        day = _safe_date(int(iso_day.group(1)), int(iso_day.group(2)), int(iso_day.group(3)))
        return day, day + timedelta(days=1)

    month_match = _ISO_MONTH.match(phrase) or _CN_MONTH.match(phrase)
    if month_match:
        start = _safe_date(int(month_match.group(1)), int(month_match.group(2)), 1)
        return start, _add_month(start)
    return None


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise TimeExpressionError("time expression is not a valid calendar date") from exc


def _quarter_start(value: date) -> date:
    return value.replace(month=((value.month - 1) // 3) * 3 + 1, day=1)


def _add_month(value: date) -> date:
    return (
        value.replace(year=value.year + 1, month=1)
        if value.month == 12
        else value.replace(month=value.month + 1)
    )


def _subtract_month(value: date) -> date:
    return (
        value.replace(year=value.year - 1, month=12)
        if value.month == 1
        else value.replace(month=value.month - 1)
    )


def _to_utc(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)


__all__ = ["TimeExpressionError", "resolve_time_expression"]
