"""ResultTable and the versioned metric registry.

Every number in a ``ResultTable`` is traceable to its source operations and a
named, versioned metric. Temporary assumptions about unconfirmed business
formulas are registered explicitly instead of being silently baked in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from factory_agent.domain.errors import InvalidRequestError


class MetricDefinition(BaseModel):
    """One named metric with an explicit version and status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    description: str
    assumption_status: str = "mock_formula_pending_customer_confirmation"


class MetricRegistry:
    """Immutable registry of reviewed metrics; lookup requires name+version."""

    def __init__(self, metrics: tuple[MetricDefinition, ...] = ()) -> None:
        self._metrics: dict[tuple[str, str], MetricDefinition] = {}
        for metric in metrics:
            self._metrics[(metric.name, metric.version)] = metric

    def register(self, metric: MetricDefinition) -> None:
        self._metrics[(metric.name, metric.version)] = metric

    def resolve(self, name: str, version: str) -> MetricDefinition:
        try:
            return self._metrics[(name, version)]
        except KeyError as error:
            raise InvalidRequestError(f"metric is not registered: {name}@{version}") from error

    def has(self, name: str, version: str) -> bool:
        return (name, version) in self._metrics


def default_metric_registry() -> MetricRegistry:
    """Mock-formula metrics pending customer confirmation (Q01~Q06 open)."""
    return MetricRegistry(
        metrics=(
            MetricDefinition(
                name="output_quantity",
                version="mock-quantity-v1",
                description="Sum of qualified quantity over piecework records",
            ),
            MetricDefinition(
                name="piecework_wage",
                version="mock-wage-v1",
                description="Sum of piecework record amounts",
            ),
            MetricDefinition(
                name="plan_progress",
                version="mock-progress-v1",
                description="Completed quantity over planned quantity per plan",
            ),
            MetricDefinition(
                name="order_achievement",
                version="mock-achievement-v1",
                description="Completed quantity over ordered quantity per order",
            ),
            MetricDefinition(
                name="payroll_total",
                version="mock-payroll-v1",
                description="Gross amount over payroll settlements",
            ),
            MetricDefinition(
                name="alert_threshold",
                version="mock-alert-v1",
                description="Bounded alert rule placeholder pending Q06",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ResultColumnMeta:
    name: str
    metric_name: str | None
    metric_version: str | None
    source_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultTable:
    """Typed result with full provenance for every number."""

    capability_id: str
    columns: tuple[ResultColumnMeta, ...]
    rows: tuple[dict[str, Any], ...]
    totals: dict[str, Decimal]
    source_operations: tuple[str, ...]
    incomplete: bool = False
    incomplete_reason: str | None = None

    def trace_for(self, column_name: str) -> ResultColumnMeta:
        for column in self.columns:
            if column.name == column_name:
                if column.metric_name is None or column.metric_version is None:
                    raise InvalidRequestError(f"column {column_name} lacks metric provenance")
                return column
        raise InvalidRequestError(f"unknown column: {column_name}")


__all__ = [
    "MetricDefinition",
    "MetricRegistry",
    "ResultColumnMeta",
    "ResultTable",
    "default_metric_registry",
]
