from datetime import datetime
from typing import Any, cast

import pytest

from factory_agent.application.cache import AuthAwareCache, CachedDirectorySource
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.directory import CachedEmployeeRosterFetcher, MesDirectorySource
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import NarrowedFilters, TenantId
from factory_agent.domain.errors import UpstreamInvalidError
from factory_agent.ports.contracts import ResourceFetchResult
from tests.support.cache import InMemoryCacheStore


class RosterAdapter:
    def __init__(self, complete: bool) -> None:
        self.complete = complete
        self.calls = 0

    async def fetch_resource(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
        extra_params: dict[str, Any] | None = None,
    ) -> ResourceFetchResult:
        self.calls += 1
        assert operation_id == "EmployeeQuery"
        return ResourceFetchResult(
            rows=(
                {
                    "uid": "fixture-employee",
                    "uname": "样例员工",
                    "dept": "fixture-department",
                    "mobile": "synthetic-private-canary",
                    "movepassword": "synthetic-private-canary",
                },
            ),
            total=1 if self.complete else 3,
            pages_fetched=1,
            complete=self.complete,
            reason=None if self.complete else "page_budget_exhausted",
        )


def build_roster(
    adapter: RosterAdapter,
) -> tuple[CachedEmployeeRosterFetcher, InMemoryCacheStore]:
    store = InMemoryCacheStore()
    cache = AuthAwareCache(
        store, contract_version="fixture", metric_version="fixture", data_version="fixture"
    )
    directory = CachedDirectorySource(
        MesDirectorySource(cast(HongzhaoMesAdapter, adapter), load_catalog()), cache
    )
    return CachedEmployeeRosterFetcher(directory), store


@pytest.mark.asyncio
async def test_incomplete_roster_cannot_be_cached_or_presented_as_a_complete_headcount() -> None:
    adapter = RosterAdapter(complete=False)
    roster, store = build_roster(adapter)
    filters = NarrowedFilters(
        tenant_id=TenantId("fixture-factory"), employee_ids=None, dept_ids=None
    )
    with pytest.raises(UpstreamInvalidError, match="not provably complete"):
        await roster.fetch_roster(filters)
    assert store.values == {}
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_shared_roster_keeps_department_and_excludes_private_fields() -> None:
    adapter = RosterAdapter(complete=True)
    roster, store = build_roster(adapter)
    filters = NarrowedFilters(
        tenant_id=TenantId("fixture-factory"), employee_ids=None, dept_ids=None
    )
    cold = await roster.fetch_roster(filters)
    hot = await roster.fetch_roster(filters)
    assert cold.rows == hot.rows
    assert hot.rows[0]["dept"] == "fixture-department"
    assert hot.cache_source == "directory"
    assert hot.complete
    assert adapter.calls == 1
    assert "synthetic-private-canary" not in repr(hot.rows)
    assert all(b"synthetic-private-canary" not in value for value in store.values.values())
