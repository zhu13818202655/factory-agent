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
    metric = registry.resolve("piecework_wage", "mock-wage-v1")
    assert metric.name == "piecework_wage"


def test_unregistered_metric_is_rejected() -> None:
    registry = default_metric_registry()
    with pytest.raises(InvalidRequestError):
        registry.resolve("unknown_metric", "v1")


def test_wrong_version_of_registered_metric_is_rejected() -> None:
    registry = default_metric_registry()
    with pytest.raises(InvalidRequestError):
        registry.resolve("piecework_wage", "v999")


def test_result_table_numbers_trace_to_operations_and_metrics() -> None:
    table = ResultTable(
        capability_id="smoke_piecework_summary",
        columns=(
            ResultColumnMeta(
                name="qualified_quantity_total",
                metric_name="output_quantity",
                metric_version="mock-quantity-v1",
                source_operations=("C1_listPieceworkRecords",),
            ),
            ResultColumnMeta(
                name="amount_total",
                metric_name="piecework_wage",
                metric_version="mock-wage-v1",
                source_operations=("C1_listPieceworkRecords",),
            ),
        ),
        rows=({"qualified_quantity_total": 8, "amount_total": Decimal("10.00")},),
        totals={"qualified_quantity_total": Decimal(8), "amount_total": Decimal("10.00")},
        source_operations=("C1_listPieceworkRecords",),
    )
    trace = table.trace_for("amount_total")
    assert trace.metric_name == "piecework_wage"
    assert "C1_listPieceworkRecords" in trace.source_operations


def test_column_without_metric_provenance_cannot_be_traced() -> None:
    table = ResultTable(
        capability_id="cap",
        columns=(
            ResultColumnMeta(
                name="raw_count",
                metric_name=None,
                metric_version=None,
                source_operations=("C2_listEmployees",),
            ),
        ),
        rows=(),
        totals={},
        source_operations=("C2_listEmployees",),
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


def test_mock_metrics_carry_assumption_status() -> None:
    registry = default_metric_registry()
    metric = registry.resolve("plan_progress", "mock-progress-v1")
    assert "pending" in metric.assumption_status


def test_duplicate_registration_overwrites_explicitly() -> None:
    registry = MetricRegistry()
    registry.register(MetricDefinition(name="m", version="v1", description="first"))
    registry.register(MetricDefinition(name="m", version="v1", description="second"))
    assert registry.resolve("m", "v1").description == "second"
