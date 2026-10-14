from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from factory_agent.domain.errors import InvalidRequestError


@dataclass(frozen=True, slots=True)
class ResultColumnMeta:
    name: str
    metric_name: str | None
    metric_version: str | None
    source_operations: tuple[str, ...]
    column_type: str | None = None
    unit: str | None = None
    #: Worker-facing Chinese display label; ``name`` stays the stable identifier.
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ResultDetailTable:
    view_id: str
    title: str
    group_by: str
    columns: tuple[ResultColumnMeta, ...]
    rows: tuple[dict[str, Any], ...]
    status_column: str | None = None
    hidden_columns: tuple[str, ...] = ()


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
    #: Front-end card payload built by the kernel when the recipe declares a
    #: ``card:`` block; ``None`` = recipe has no card or the result is empty.
    card: dict[str, object] | None = None
    detail_tables: tuple[ResultDetailTable, ...] = ()
    hidden_columns: tuple[str, ...] = ()

    def trace_for(self, column_name: str) -> ResultColumnMeta:
        for column in self.columns:
            if column.name == column_name:
                if column.metric_name is None or column.metric_version is None:
                    raise InvalidRequestError(f"column {column_name} lacks metric provenance")
                return column
        raise InvalidRequestError(f"unknown column: {column_name}")
