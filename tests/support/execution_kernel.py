"""In-process stubs for execution kernel unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.execution.executor import ScopedExecutor


@dataclass
class RecordingAdapter:
    """Minimal MesDataSource double that records the scope flow and returns rows.

    Implements the ``ResourceFetcher`` port the ``ScopedExecutor`` consumes.
    """

    rows: tuple[dict[str, Any], ...] = ()
    requests: list[tuple[str, NarrowedFilters, tuple[datetime, datetime], int]] = field(
        default_factory=lambda: []
    )

    async def fetch_resource_rows(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
    ) -> list[dict[str, Any]]:
        self.requests.append((operation_id, filters, time_range, page_size))
        return list(self.rows)


def make_scoped_executor() -> tuple[RecordingAdapter, ScopedExecutor]:
    adapter = RecordingAdapter(
        rows=(
            {
                "uid": "01001",
                "uname": "模拟员工甲",
                "dept": "dept-a1",
                "worktype": "WT01",
                "sl": "3",
                "price": "1.2500",
                "je": "3.7500",
            },
        )
    )
    catalog = load_catalog()
    return adapter, ScopedExecutor(adapter=adapter, catalog=catalog)


def fixed_instant() -> datetime:
    return datetime(2026, 8, 21, tzinfo=UTC)


__all__ = ["RecordingAdapter", "fixed_instant", "make_scoped_executor"]
