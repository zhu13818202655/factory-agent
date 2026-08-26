"""ResultTable and the versioned metric registry.

Every number in a ``ResultTable`` is traceable to its source operations and a
named, versioned metric. Temporary assumptions about unconfirmed business
formulas are registered explicitly instead of being silently baked in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from factory_agent.domain.errors import InvalidRequestError


class MetricDefinition(BaseModel):
    """One named metric with an explicit version and confirmation status.

    ``status`` follows the Story 5 registry contract: ``confirmed`` metrics may
    participate in numeric computation; ``unconfirmed`` and ``unavailable``
    metrics must surface as an explicit ``unavailable`` column state instead of
    a fabricated number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    description: str
    status: Literal["confirmed", "unconfirmed", "unavailable"] = "unconfirmed"
    assumption_status: str = ""

    def allows_numeric_rendering(self) -> bool:
        return self.status == "confirmed"


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
    """Story 5 registry: customer-confirmed formulas plus explicit gaps.

    Confirmed metrics come from M6/M9/M18. Unconfirmed/unavailable entries
    mirror chapter-5 open items (C.5/C.7/C.8/C.9/C.12) and must never be
    rendered as numbers.
    """
    return MetricRegistry(
        metrics=(
            MetricDefinition(
                name="payroll_amount",
                version="customer-payroll-v1",
                description="Piecework wage per row: sl x price",
                status="confirmed",
                assumption_status="M9/M18 confirmed",
            ),
            MetricDefinition(
                name="payroll_gross_total",
                version="customer-footer-v1",
                description="Gross payable total from footer.je_total",
                status="confirmed",
                assumption_status="M9/M13 confirmed",
            ),
            MetricDefinition(
                name="payroll_piece_count",
                version="customer-payroll-v1",
                description="Piecework quantity: sum of sl over the window",
                status="confirmed",
                assumption_status="M9/M18 confirmed",
            ),
            MetricDefinition(
                name="payroll_unit_price",
                version="customer-payroll-v1",
                description="Piecework unit price for one wage detail row",
                status="confirmed",
                assumption_status="M9/M18 confirmed",
            ),
            MetricDefinition(
                name="payroll_daily_average",
                version="factory-daily-average-v1",
                description="Daily average wage: gross total over natural days",
                status="confirmed",
                assumption_status="factory-defined; non-customer denominator",
            ),
            MetricDefinition(
                name="output_personal",
                version="customer-output-v1",
                description="Personal output: sum of sl in Ysk/BarcodeCl context",
                status="confirmed",
                assumption_status="M18; fine-grained wording pending C.10",
            ),
            MetricDefinition(
                name="output_order_completed",
                version="customer-completed-v1",
                description="Order completed quantity: sum of sssl (Sclzd context)",
                status="confirmed",
                assumption_status="M18; fine-grained wording pending C.10",
            ),
            MetricDefinition(
                name="progress_ratio",
                version="customer-progress-v1",
                description="Scanned worktype count over total worktype count",
                status="confirmed",
                assumption_status="M6/M18 confirmed",
            ),
            MetricDefinition(
                name="quality_defective",
                version="unavailable-c5",
                description="Defective quantity: manual-entry cp only, no unified source",
                status="unavailable",
                assumption_status="chapter 5 C.5 unanswered",
            ),
            MetricDefinition(
                name="plan_target_output",
                version="unavailable-c9",
                description="Target output / achievement rate has no data source",
                status="unavailable",
                assumption_status="chapter 5 C.9 unanswered",
            ),
            MetricDefinition(
                name="org_headcount",
                version="unavailable-c7",
                description="Registered headcount lacks active/inactive field",
                status="unavailable",
                assumption_status="chapter 5 C.7 unanswered",
            ),
            MetricDefinition(
                name="production_stage",
                version="unavailable-c8",
                description="Mass-production stage has no data source",
                status="unavailable",
                assumption_status="chapter 5 C.8 unanswered",
            ),
            MetricDefinition(
                name="time_flag_default",
                version="unconfirmed-c12",
                description="GongziMxQuery Flag default (0 scan date / 1 review date)",
                status="unconfirmed",
                assumption_status="chapter 5 C.12 unanswered",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ResultColumnMeta:
    name: str
    metric_name: str | None
    metric_version: str | None
    source_operations: tuple[str, ...]
    column_type: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class ResultTable:
    """Typed result with full provenance for every number."""

    capability_id: str
    columns: tuple[ResultColumnMeta, ...]
    rows: tuple[dict[str, Any], ...]
    totals: dict[str, Decimal]
    source_operations: tuple[str, ...]
    warnings: tuple[str, ...] = ()
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
