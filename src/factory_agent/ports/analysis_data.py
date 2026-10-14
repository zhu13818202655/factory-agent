from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sageql_core import DatasetDescriptor, Query

from factory_agent.domain import DataScope, NarrowedFilters, Role, TimeRange

AnalysisValue = str | Decimal | int | bool | None


@dataclass(frozen=True, slots=True)
class FactQuery:
    dataset_id: str
    columns: tuple[str, ...]
    filters: NarrowedFilters
    scope: DataScope
    role: Role
    time_range: TimeRange
    time_basis: str = "reported"
    source_type: str = "all"


@dataclass(frozen=True, slots=True)
class FactBatch:
    dataset_id: str
    rows: tuple[Mapping[str, AnalysisValue], ...]
    complete: bool
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    generated_at: datetime | None = None
    cache_source: str = "live"
    source_calls: int = 0
    reason: str | None = None


class FactSource(Protocol):
    async def fetch(self, query: FactQuery) -> FactBatch: ...


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    reference: str
    query: Query
    table_name: str
    rows: tuple[Mapping[str, AnalysisValue], ...]
    columns: tuple[str, ...]
    descriptor: DatasetDescriptor
