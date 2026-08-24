"""In-process stubs for execution kernel unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from factory_agent.data_api.canonical import CanonicalRequest, fetch_resource_rows
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain.queries import ResourceQuery
from factory_agent.execution.executor import ScopedExecutor


@dataclass
class RecordingAdapter:
    """Minimal MesDataSource double that records requests and returns rows."""

    rows: tuple[dict[str, Any], ...] = ()
    requests: list[CanonicalRequest] = field(default_factory=lambda: [])

    async def execute(self, request: CanonicalRequest) -> Any:
        from pydantic import BaseModel

        class _Page(BaseModel):
            items: list[dict[str, Any]]
            total: int
            page: int
            size: int

        self.requests.append(request)
        return _Page(items=list(self.rows), total=len(self.rows), page=1, size=len(self.rows))

    async def fetch_resource_rows(
        self, operation_id: str, query: ResourceQuery
    ) -> list[dict[str, Any]]:
        """ResourceFetcher port implementation backed by the real fetch path."""

        return await fetch_resource_rows(
            self,  # type: ignore[arg-type]
            operation_id,
            query,
        )


def make_scoped_executor() -> tuple[RecordingAdapter, ScopedExecutor]:
    adapter = RecordingAdapter(
        rows=(
            {
                "record_id": "stub-1",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a1",
                "dept_id": "group-a1",
                "order_id": "order-a1",
                "style_id": "style-a1",
                "operation_id": "operation-a1",
                "plan_id": None,
                "work_at": "2026-08-20T10:00:00Z",
                "completed_quantity": "3",
                "qualified_quantity": "3",
                "defective_quantity": "0",
                "unit_rate": "1.2500",
                "amount": "3.7500",
                "status": "reported",
            },
        )
    )
    catalog = load_catalog()
    return adapter, ScopedExecutor(adapter=adapter, catalog=catalog)  # type: ignore[arg-type]


def fixed_instant() -> datetime:
    return datetime(2026, 8, 21, tzinfo=UTC)


__all__ = ["RecordingAdapter", "fixed_instant", "make_scoped_executor"]
