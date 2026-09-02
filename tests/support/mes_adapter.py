"""In-process fake MES adapter for kernel tests.

The fake replays deterministic page sequences, including fault scenarios
(duplicate pages, missing pages, total drift), and records every request so
tests can assert zero-call guarantees. It speaks the customer envelope
(``result.list`` / ``result.total``) through the ``MesRequest`` / ``MesResponse``
boundary.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from factory_agent.data_api.hongzhao import MesRequest, MesResponse


@dataclass(frozen=True, slots=True)
class FakePage:
    items: tuple[dict[str, Any], ...]
    total: int | None = None


@dataclass(frozen=True, slots=True)
class FaultScript:
    """Deterministic fault injection keyed by page number."""

    duplicate_page: frozenset[int] = field(default_factory=lambda: frozenset[int]())
    missing_pages: frozenset[int] = field(default_factory=lambda: frozenset[int]())
    wrong_total: dict[int, int] = field(default_factory=lambda: dict[int, int]())
    null_fields: frozenset[int] = field(default_factory=lambda: frozenset[int]())
    extra_fields: frozenset[int] = field(default_factory=lambda: frozenset[int]())


@dataclass(frozen=True, slots=True)
class FakeOperation:
    """Page sequence for one operation; ``pages`` drives the fake envelope."""

    item_model: type[BaseModel]
    pages: tuple[FakePage, ...]
    total: int
    faults: FaultScript = field(default_factory=FaultScript)


@dataclass
class FakeMesAdapter:
    """Records requests and replays scripted customer-shaped responses."""

    operations: dict[str, FakeOperation] = field(default_factory=lambda: {})
    requests: list[MesRequest] = field(default_factory=lambda: [])
    raise_error: Exception | None = None

    async def execute(self, request: MesRequest) -> MesResponse:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        operation = self.operations.get(request.operation_id)
        if operation is None:
            raise AssertionError(f"unexpected operation requested: {request.operation_id}")
        page_number = int(request.params.get("page", 1))
        return self._page_response(operation, page_number)

    def _page_response(self, operation: FakeOperation, page_number: int) -> MesResponse:
        index = min(page_number, len(operation.pages)) - 1
        source = operation.pages[index]
        faults = operation.faults
        raw_items: list[dict[str, Any]] = [deepcopy(item) for item in source.items]

        if page_number in faults.missing_pages:
            raw_items = []
        elif page_number in faults.duplicate_page and raw_items:
            raw_items.append(deepcopy(raw_items[0]))
        if page_number in faults.null_fields and raw_items:
            first = raw_items[0]
            target = next((key for key in first if key.endswith("_id")), next(iter(first)))
            first[target] = None
        if page_number in faults.extra_fields and raw_items:
            raw_items[0]["synthetic_drift_field"] = "unexpected"

        total = faults.wrong_total.get(page_number, source.total or operation.total)
        return MesResponse(result={"list": raw_items, "total": total}, footer=None)

    def calls_for(self, operation_id: str) -> list[MesRequest]:
        return [request for request in self.requests if request.operation_id == operation_id]


__all__ = ["FakeMesAdapter", "FakeOperation", "FakePage", "FaultScript"]
