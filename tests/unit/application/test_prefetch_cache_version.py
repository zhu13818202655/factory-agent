from datetime import datetime
from typing import Any, Mapping

import pytest
from pydantic import RedisDsn

import factory_agent.bootstrap as bootstrap
from factory_agent.application.cache import AuthAwareCache
from factory_agent.application.prefetch import V2PrefetchWorker
from factory_agent.config import FactoryAgentSettings
from factory_agent.ports.contracts import ResourceFetchResult
from tests.support.cache import InMemoryCacheStore
from tests.unit.application.test_prefetch import CST, FakeResultCache


@pytest.mark.asyncio
async def test_production_cache_version_refreshes_despite_old_same_day_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryCacheStore()

    def memory_store(_: str) -> InMemoryCacheStore:
        return store

    monkeypatch.setattr(bootstrap, "RedisCacheStore", memory_store)
    settings = FactoryAgentSettings(redis_url=RedisDsn("redis://redis-synthetic:6379/0"))
    current = bootstrap._build_cache(settings)  # pyright: ignore[reportPrivateUsage]
    assert current is not None
    previous = AuthAwareCache(
        store,
        contract_version="mes-contract-v2",
        metric_version="metric-registry-v1",
        data_version="mes-detail-v2",
    )
    calls: list[str] = []

    async def fetch(
        operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult:
        calls.append(operation_id)
        return ResourceFetchResult(
            rows=({"fixture": "row"},), total=1, pages_fetched=1, complete=True
        )

    now = datetime(2026, 10, 13, 13, tzinfo=CST)
    old_worker = V2PrefetchWorker(
        fetch, FakeResultCache(), "SYNTHETIC-FACTORY", coordination=previous
    )
    assert await old_worker.run_once(now) == {"GongziClQuery": 1, "ScjdQuery": 1}
    assert await old_worker.run_once(now) == {}
    old_keys = set(store.values)
    assert all(":mes-detail-v2:" in key for key in old_keys)

    current_worker = V2PrefetchWorker(
        fetch, FakeResultCache(), "SYNTHETIC-FACTORY", coordination=current
    )
    assert await current_worker.run_once(now) == {"GongziClQuery": 1, "ScjdQuery": 1}
    assert await current_worker.run_once(now) == {}
    assert calls == ["GongziClQuery", "ScjdQuery", "GongziClQuery", "ScjdQuery"]
    assert old_keys <= set(store.values)
    assert all(":mes-detail-v4:" in key for key in set(store.values) - old_keys)
