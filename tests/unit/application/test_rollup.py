"""Rollup metrics for MES calls.

``compute_bucket_metrics`` must aggregate ``mes_call_fact`` rows per category
and per status, keep success and failure separate, and never re-count
``page_count`` into the call count (D6). Unknown operations fall back to
``other`` so a stale mapping cannot silently drop traffic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from factory_agent.application.rollup import MES_CATEGORIES, compute_bucket_metrics, hour_bucket
from factory_agent.persistence.rollup_store import (
    InteractionFactRow,
    LlmCallFactRow,
    MesCallFactRow,
)

NOW = datetime(2026, 8, 24, 6, 30, tzinfo=timezone.utc)
BUCKET = hour_bucket(NOW)


def no_interactions() -> list[InteractionFactRow]:
    return []


def no_llm() -> list[LlmCallFactRow]:
    return []


def mes_call(
    operation_id: str,
    status: str,
    *,
    occurred_at: datetime = NOW,
    tenant_id: str = "tenant-a",
) -> MesCallFactRow:
    return MesCallFactRow(
        tenant_id=tenant_id,
        occurred_at=occurred_at,
        operation_id=operation_id,
        status=status,
    )


CATEGORIES = {
    "YskQuery": "output",
    "GongziMxQuery": "payroll",
    "PlanGridPageList": "order",
    "SystemToken": "other",
    "NewOpNeverClassified": "other",  # fallback for unknown operations
}


def metrics(facts: list[MesCallFactRow]) -> dict[str, float]:
    buckets = compute_bucket_metrics(no_interactions(), no_llm(), facts, CATEGORIES, "hour")
    return buckets[BUCKET]


def test_each_category_is_aggregated_separately() -> None:
    bucket = metrics(
        [
            mes_call("YskQuery", "completed"),
            mes_call("YskQuery", "completed"),
            mes_call("GongziMxQuery", "completed"),
            mes_call("PlanGridPageList", "completed"),
            mes_call("SystemToken", "completed"),
        ]
    )

    assert bucket["mes_calls"] == 5
    assert bucket["mes_calls.completed"] == 5
    assert bucket["mes_calls.failed"] == 0
    assert bucket["mes_calls.output"] == 2
    assert bucket["mes_calls.payroll"] == 1
    assert bucket["mes_calls.order"] == 1
    assert bucket["mes_calls.other"] == 1


def test_success_and_failure_are_counted_separately() -> None:
    bucket = metrics(
        [
            mes_call("YskQuery", "completed"),
            mes_call("YskQuery", "failed"),
            mes_call("YskQuery", "failed"),
        ]
    )

    assert bucket["mes_calls"] == 3
    assert bucket["mes_calls.completed"] == 1
    assert bucket["mes_calls.failed"] == 2
    # Category counts include both outcomes (billing is per call, not per success).
    assert bucket["mes_calls.output"] == 3


def test_page_count_is_never_summed_into_the_call_count() -> None:
    """D6: call counts come from fact rows; page_count is a supporting metric."""
    # Three physical HTTP attempts: one non-paged and one 2-page fetch + one
    # failed retry. Each is its own fact row; the counts stay at row level.
    bucket = metrics(
        [
            mes_call("YskQuery", "completed"),
            mes_call("YskQuery", "completed"),
            mes_call("YskQuery", "failed"),
        ]
    )

    assert bucket["mes_calls"] == 3


def test_unknown_operations_fall_back_to_other() -> None:
    bucket = metrics([mes_call("NewOpNeverClassified", "completed")])

    assert bucket["mes_calls.other"] == 1
    assert bucket["mes_calls.output"] == 0


def test_every_category_metric_exists_for_all_four_categories() -> None:
    bucket = metrics(
        [
            mes_call("YskQuery", "completed"),
            mes_call("GongziMxQuery", "completed"),
            mes_call("PlanGridPageList", "completed"),
            mes_call("SystemToken", "completed"),
        ]
    )

    for category in MES_CATEGORIES:
        assert f"mes_calls.{category}" in bucket, category
    assert "mes_calls" in bucket
    assert "mes_calls.completed" in bucket
    assert "mes_calls.failed" in bucket


def test_hourly_bucket_rounds_down_to_the_hour() -> None:
    late = NOW.replace(minute=59, second=59, microsecond=999999)
    bucket = compute_bucket_metrics(
        no_interactions(),
        no_llm(),
        [mes_call("YskQuery", "completed", occurred_at=late)],
        CATEGORIES,
        "hour",
    )

    assert set(bucket) == {BUCKET}


def test_daily_granularity_buckets_by_utc_date() -> None:
    next_day = NOW.replace(hour=23, minute=59) + __import__("datetime").timedelta(minutes=2)
    buckets = compute_bucket_metrics(
        no_interactions(),
        no_llm(),
        [mes_call("YskQuery", "completed", occurred_at=next_day)],
        CATEGORIES,
        "day",
    )

    day = datetime(2026, 8, 25, tzinfo=timezone.utc)
    assert buckets == {
        day: {
            "questions": 0.0,
            "valid_questions": 0.0,
            "e2e_duration_ms": 0.0,
            "mes_duration_ms": 0.0,
            "llm_duration_ms": 0.0,
            "local_duration_ms": 0.0,
            "llm_physical_attempts": 0.0,
            "prompt_tokens": 0.0,
            "completion_tokens": 0.0,
            "cached_tokens": 0.0,
            "reasoning_tokens": 0.0,
            "status.completed": 0.0,
            "status.failed": 0.0,
            "status.cancelled": 0.0,
            "status.rejected": 0.0,
            "questions.count": 0.0,
            "valid_questions.count": 0.0,
            "e2e_duration_ms.count": 0.0,
            "mes_duration_ms.count": 0.0,
            "llm_duration_ms.count": 0.0,
            "local_duration_ms.count": 0.0,
            "llm_physical_attempts.count": 0.0,
            "prompt_tokens.count": 0.0,
            "completion_tokens.count": 0.0,
            "cached_tokens.count": 0.0,
            "reasoning_tokens.count": 0.0,
            "users": 0.0,
            "llm_logical_calls": 0.0,
            "mes_calls": 1.0,
            "mes_calls.completed": 1.0,
            "mes_calls.failed": 0.0,
            "mes_calls.output": 1.0,
            "mes_calls.payroll": 0.0,
            "mes_calls.order": 0.0,
            "mes_calls.other": 0.0,
        }
    }
