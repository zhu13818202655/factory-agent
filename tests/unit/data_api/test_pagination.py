from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from factory_agent.data_api.pagination import BoundedPager, PagerBudget
from tests.support.mes_adapter import FakeMesAdapter, FakeOperation, FakePage, FaultScript


class _Item(BaseModel):
    model_config = {"extra": "forbid"}
    record_id: str


def _operation(
    pages: tuple[FakePage, ...], total: int, faults: FaultScript | None = None
) -> FakeOperation:
    return FakeOperation(item_model=_Item, pages=pages, total=total, faults=faults or FaultScript())


def _adapter(operation_id: str, operation: FakeOperation) -> FakeMesAdapter:
    return FakeMesAdapter(operations={operation_id: operation})


def _pager(adapter: Any, budget: PagerBudget | None = None) -> BoundedPager:
    """Wrap any adapter double; the pager only needs ``execute``."""
    effective_budget = budget if budget is not None else PagerBudget()
    return BoundedPager(adapter, budget=effective_budget)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_single_page_fetch_is_proven_complete() -> None:
    adapter = _adapter("YskQuery", _operation((FakePage(items=({"record_id": "r1"},)),), 1))
    pager = _pager(adapter)
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is True
    assert len(result.items) == 1
    assert result.total == 1


@pytest.mark.asyncio
async def test_multi_page_sequential_fetch_proves_completion() -> None:
    pages = (
        FakePage(items=tuple({"record_id": f"r{i}"} for i in range(1, 4))),
        FakePage(items=tuple({"record_id": f"r{i}"} for i in range(4, 6))),
    )
    adapter = _adapter("YskQuery", _operation(pages, total=5))
    pager = _pager(adapter)
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is True
    assert len(result.items) == 5
    assert result.pages_fetched == 2


@pytest.mark.asyncio
async def test_duplicate_page_aborts_with_structured_status() -> None:
    """Page 2 repeats page 1's rows; the pager must abort, not double count."""
    pages = (
        FakePage(items=({"record_id": "r1"}, {"record_id": "r2"})),
        FakePage(items=({"record_id": "r1"}, {"record_id": "r2"})),
    )
    adapter = _adapter("YskQuery", _operation(pages, total=4))
    pager = _pager(adapter)
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is False
    assert result.reason == "duplicate_page"


@pytest.mark.asyncio
async def test_missing_page_detected_against_total() -> None:
    pages = (FakePage(items=()),)
    adapter = _adapter("YskQuery", _operation(pages, total=5))
    pager = _pager(adapter)
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is False
    assert result.reason == "missing_pages"


@pytest.mark.asyncio
async def test_total_drift_aborts_with_structured_status() -> None:
    pages = (
        FakePage(items=({"record_id": "r1"},)),
        FakePage(items=({"record_id": "r2"},)),
    )
    adapter = _adapter(
        "YskQuery",
        _operation(pages, total=2, faults=FaultScript(wrong_total={2: 9})),
    )
    pager = _pager(adapter)
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is False
    assert result.reason == "total_drift"


@pytest.mark.asyncio
async def test_page_budget_exhaustion_returns_incomplete() -> None:
    # 10 distinct single-row pages against total 10; budget stops at 3 pages.
    pages = tuple(FakePage(items=({"record_id": f"r{n}"},)) for n in range(10))
    adapter = _adapter("YskQuery", _operation(pages, total=10))
    pager = _pager(adapter, budget=PagerBudget(max_pages=3, max_rows=100))
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is False
    assert result.reason == "page_budget_exhausted"


@pytest.mark.asyncio
async def test_row_budget_exhaustion_returns_incomplete() -> None:
    pages = tuple(
        FakePage(items=tuple({"record_id": f"r{p}{i}"} for i in range(50))) for p in range(5)
    )
    adapter = _adapter("YskQuery", _operation(pages, total=250))
    pager = _pager(adapter, budget=PagerBudget(max_pages=20, max_rows=120))
    result = await pager.fetch_all("YskQuery", {}, item_model=_Item)
    assert result.complete is False
    assert result.reason == "row_budget_exhausted"
    assert len(result.items) <= 120


@pytest.mark.asyncio
async def test_null_field_drift_raises_upstream_invalid() -> None:
    """A null ID field inside an item must fail validation upstream-side."""
    from factory_agent.data_api.hongzhao import MesRequest, MesResponse
    from factory_agent.domain.errors import UpstreamInvalidError

    class _BrokenAdapter(FakeMesAdapter):
        async def execute(self, request: MesRequest) -> MesResponse:
            self.requests.append(request)
            return MesResponse(result={"list": [{"record_id": None}], "total": 1}, footer=None)

    pager = _pager(_BrokenAdapter())
    with pytest.raises(UpstreamInvalidError):
        await pager.fetch_all("YskQuery", {}, item_model=_Item)


@pytest.mark.asyncio
async def test_extra_field_drift_raises_upstream_invalid() -> None:
    """Unknown fields in items are rejected by the strict item model."""
    from factory_agent.data_api.hongzhao import MesRequest, MesResponse
    from factory_agent.domain.errors import UpstreamInvalidError

    class _DriftAdapter(FakeMesAdapter):
        async def execute(self, request: MesRequest) -> MesResponse:
            self.requests.append(request)
            return MesResponse(
                result={
                    "list": [{"record_id": "r1", "synthetic_drift_field": "unexpected"}],
                    "total": 1,
                },
                footer=None,
            )

    pager = _pager(_DriftAdapter())
    with pytest.raises(UpstreamInvalidError):
        await pager.fetch_all("YskQuery", {}, item_model=_Item)
