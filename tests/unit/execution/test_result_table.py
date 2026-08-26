from __future__ import annotations

from decimal import Decimal

import pytest

from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.result_table import (
    MetricDefinition,
    MetricRegistry,
    ResultColumnMeta,
    ResultTable,
    default_metric_registry,
)


def test_metric_registry_resolves_name_and_version() -> None:
    registry = default_metric_registry()
    metric = registry.resolve("payroll_amount", "customer-payroll-v1")
    assert metric.name == "payroll_amount"
    assert metric.allows_numeric_rendering() is True


def test_unregistered_metric_is_rejected() -> None:
    registry = default_metric_registry()
    with pytest.raises(InvalidRequestError):
        registry.resolve("unknown_metric", "v1")


def test_wrong_version_of_registered_metric_is_rejected() -> None:
    registry = default_metric_registry()
    with pytest.raises(InvalidRequestError):
        registry.resolve("payroll_amount", "v999")


def test_result_table_numbers_trace_to_operations_and_metrics() -> None:
    table = ResultTable(
        capability_id="smoke_piecework_summary",
        columns=(
            ResultColumnMeta(
                name="output_total",
                metric_name="output_personal",
                metric_version="customer-output-v1",
                source_operations=("YskQuery", "EmployeeQuery"),
            ),
            ResultColumnMeta(
                name="amount_total",
                metric_name="payroll_amount",
                metric_version="customer-payroll-v1",
                source_operations=("GongziMxQuery",),
            ),
        ),
        rows=({"output_total": 8, "amount_total": Decimal("10.00")},),
        totals={"output_total": Decimal(8), "amount_total": Decimal("10.00")},
        source_operations=("YskQuery", "GongziMxQuery"),
    )
    trace = table.trace_for("amount_total")
    assert trace.metric_name == "payroll_amount"
    assert "GongziMxQuery" in trace.source_operations


def test_column_without_metric_provenance_cannot_be_traced() -> None:
    table = ResultTable(
        capability_id="cap",
        columns=(
            ResultColumnMeta(
                name="raw_count",
                metric_name=None,
                metric_version=None,
                source_operations=("DeptQuery",),
            ),
        ),
        rows=(),
        totals={},
        source_operations=("DeptQuery",),
    )
    with pytest.raises(InvalidRequestError):
        table.trace_for("raw_count")


def test_incomplete_table_declares_status() -> None:
    table = ResultTable(
        capability_id="cap",
        columns=(),
        rows=(),
        totals={},
        source_operations=(),
        incomplete=True,
        incomplete_reason="page_budget_exhausted",
    )
    assert table.incomplete is True
    assert table.incomplete_reason == "page_budget_exhausted"


def test_unconfirmed_metrics_carry_a_gap_status() -> None:
    registry = default_metric_registry()
    metric = registry.resolve("quality_defective", "unavailable-c5")
    assert metric.status == "unavailable"
    assert not metric.allows_numeric_rendering()
    assert "C.5" in metric.assumption_status


def test_duplicate_registration_overwrites_explicitly() -> None:
    registry = MetricRegistry()
    registry.register(MetricDefinition(name="m", version="v1", description="first"))
    registry.register(MetricDefinition(name="m", version="v1", description="second"))
    assert registry.resolve("m", "v1").description == "second"
