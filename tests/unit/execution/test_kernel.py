"""Kernel capability runner vertical-slice tests (Story 6).

Uses a fake step executor returning the wages golden rows to prove that FR-002
(summary) and FR-003 (detail) share the ``GongziMxQuery`` path, that the local
aggregate reconciles against ``footer.je_total``, and that empty/pagination
anomalies surface as structured states instead of fabricated numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import CapabilityId, EmployeeId, TenantId, TimeRange
from factory_agent.execution.executor import ExecutionRequest
from factory_agent.execution.kernel import KernelCapabilityRunner
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.session import CapabilityRunRequest

_DETAIL_ROWS: tuple[dict[str, Any], ...] = (
    {
        "type": "扫码产量",
        "rq": "2026-07-31",
        "worktype": "WT01",
        "sl": "4",
        "price": "1.2500",
        "je": "5.0000",
    },
    {
        "type": "扫码产量",
        "rq": "2026-08-05",
        "worktype": "WT01",
        "sl": "5",
        "price": "1.2500",
        "je": "6.2500",
    },
    {
        "type": "扫码产量",
        "rq": "2026-08-06",
        "worktype": "WT03",
        "sl": "4",
        "price": "1.0000",
        "je": "4.0000",
    },
    {
        "type": "吊挂产量",
        "rq": "2026-08-06",
        "worktype": "WT03",
        "sl": "4",
        "price": "1.0000",
        "je": "4.0000",
    },
    {
        "type": "手工账产量",
        "rq": "2026-08-06",
        "worktype": "WT02",
        "sl": "3",
        "price": "0.8000",
        "je": "2.4000",
    },
)

_SUMMARY_ROWS: tuple[dict[str, Any], ...] = (
    {"type": "扫码产量", "worktype": "WT01", "sl": "9", "je": "11.2500"},
    {"type": "扫码产量", "worktype": "WT03", "sl": "4", "je": "4.0000"},
    {"type": "吊挂产量", "worktype": "WT03", "sl": "4", "je": "4.0000"},
    {"type": "手工账产量", "worktype": "WT02", "sl": "3", "je": "2.4000"},
)

_FOOTER = {"sl_total": "20", "je_total": "21.6500"}


class FakeStepExecutor:
    """Implements ``StepExecutor``; returns the golden wages rows by scheme."""

    def __init__(self, *, complete: bool = True, reason: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._complete = complete
        self._reason = reason

    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> ResourceFetchResult:
        self.calls.append((request.operation_id, dict(extra_params or {})))
        scheme = (extra_params or {}).get("scheme", "")
        rows = _SUMMARY_ROWS if scheme == "hz" else _DETAIL_ROWS
        return ResourceFetchResult(
            rows=tuple(rows),
            total=len(rows),
            pages_fetched=1,
            complete=self._complete,
            reason=self._reason,
            footer=_FOOTER,
        )


def _filters() -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId("01001")}),
        dept_ids=frozenset(),
    )


def _range() -> TimeRange:
    return TimeRange(
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_fr002_summary_totals_and_reconcile() -> None:
    runner = _runner()
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr002_personal_wage_summary"),
            filters=_filters(),
            time_range=_range(),
        )
    )

    assert result.column_names == ("gross_total", "piece_count", "daily_avg")
    assert result.totals["gross_total"] == Decimal("21.65")
    assert result.totals["piece_count"] == Decimal("20")
    assert result.incomplete is False
    assert (result.column_types or {})["gross_total"] == "money"
    assert result.source_operations == ("GongziMxQuery",)


@pytest.mark.asyncio
async def test_fr003_detail_matches_summary_totals() -> None:
    runner = _runner()
    detail = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr003_personal_wage_detail"),
            filters=_filters(),
            time_range=_range(),
        )
    )

    assert len(detail.rows) == 5
    assert detail.totals["je"] == Decimal("21.65")
    assert detail.totals["sl"] == Decimal("20")
    # FR-003 detail total equals the FR-002 summary gross total.
    summary = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr002_personal_wage_summary"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert summary.totals["gross_total"] == detail.totals["je"]
    # Every row honours je = sl x price.
    for row in detail.rows:
        assert Decimal(str(row[4])) == Decimal(str(row[2])) * Decimal(str(row[3]))


@pytest.mark.asyncio
async def test_reconciliation_failure_is_structured_not_silent() -> None:
    class BadFooterExecutor(FakeStepExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, str] | None = None,
        ) -> ResourceFetchResult:
            self.calls.append((request.operation_id, dict(extra_params or {})))
            return ResourceFetchResult(
                rows=tuple(_DETAIL_ROWS),
                total=len(_DETAIL_ROWS),
                pages_fetched=1,
                complete=True,
                footer={"sl_total": "20", "je_total": "9999.0000"},
            )

    runner = KernelCapabilityRunner(
        BadFooterExecutor(),
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
    )
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr003_personal_wage_detail"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.incomplete is True
    assert result.incomplete_reason == "reconciliation_failed"
    assert any("对账失败" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_empty_result_is_zero_aggregate_not_fabricated() -> None:
    class EmptyExecutor(FakeStepExecutor):
        async def execute_full_step(
            self,
            filters: Any,
            request: ExecutionRequest,
            active_scope: Any | None = None,
            extra_params: dict[str, str] | None = None,
        ) -> ResourceFetchResult:
            self.calls.append((request.operation_id, dict(extra_params or {})))
            return ResourceFetchResult(
                rows=(),
                total=0,
                pages_fetched=1,
                complete=True,
                footer={"sl_total": "0", "je_total": "0.0000"},
            )

    runner = KernelCapabilityRunner(
        EmptyExecutor(),
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
    )
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr002_personal_wage_summary"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.totals["gross_total"] == Decimal("0")
    assert result.totals["piece_count"] == Decimal("0")


@pytest.mark.asyncio
async def test_pagination_incomplete_surfaces_structured_state() -> None:
    runner = KernelCapabilityRunner(
        FakeStepExecutor(complete=False, reason="total_drift"),
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
    )
    result = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr003_personal_wage_detail"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    assert result.incomplete is True
    assert result.incomplete_reason == "pagination_total_drift"


def _runner() -> KernelCapabilityRunner:
    return KernelCapabilityRunner(
        FakeStepExecutor(),
        load_recipes(load_catalog().operation_ids),
        default_metric_registry(),
    )
