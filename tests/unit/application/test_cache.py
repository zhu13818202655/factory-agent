"""Auth-aware cache: scope fingerprints, versioned keys, fallback, invalidation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.cache import (
    AuthAwareCache,
    UnknownCacheDomainError,
)
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    ScopeVersion,
    TenantId,
)
from tests.support.cache import InMemoryCacheStore

TENANT = TenantId("tenant-a")
NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


def make_scope(version: str, *, depts: int = 1, employees: int = 1) -> DataScope:
    return DataScope(
        tenant_id=TENANT,
        employee_ids=frozenset({EmployeeId("emp-1")} if employees else {}),
        dept_ids=frozenset({DeptId("dept-1")} if depts else {}),
        evaluated_at=NOW,
        scope_version=ScopeVersion(version),
        mes_filtered=True,
    )


def make_cache(
    store: InMemoryCacheStore | None = None,
) -> tuple[AuthAwareCache, InMemoryCacheStore]:
    active_store = store or InMemoryCacheStore()
    cache = AuthAwareCache(
        active_store,
        contract_version="mes-v1",
        metric_version="metric-v1",
        data_version="data-v1",
    )
    return cache, active_store


def test_scope_fingerprint_is_irreversible_and_version_aware() -> None:
    cache, _ = make_cache()

    fingerprint = cache.scope_fingerprint(make_scope("scope-1"))

    assert fingerprint == cache.scope_fingerprint(make_scope("scope-1"))
    assert fingerprint != cache.scope_fingerprint(make_scope("scope-2"))
    # Only counts are recorded — the raw IDs never appear in the fingerprint.
    assert "emp-1" not in fingerprint
    assert "dept-1" not in fingerprint


def test_key_contains_tenant_fingerprint_versions_and_params() -> None:
    cache, _ = make_cache()
    fingerprint = cache.scope_fingerprint(make_scope("scope-1"))

    key = cache.build_key("output", TENANT, fingerprint, {"time_range": "本月"})

    assert key.startswith("fa:mes-v1:metric-v1:data-v1:tenant-a:")
    assert fingerprint in key
    assert "output" in key
    # Different params -> different key.
    other = cache.build_key("output", TENANT, fingerprint, {"time_range": "上月"})
    assert key != other


@pytest.mark.asyncio
async def test_cross_tenant_keys_can_never_hit() -> None:
    cache, _ = make_cache()
    fingerprint = cache.scope_fingerprint(make_scope("scope-1"))

    await cache.put("output", TENANT, fingerprint, {"time_range": "本月"}, b"rows")
    other_tenant = TenantId("tenant-b")
    lookup = await cache.get("output", other_tenant, fingerprint, {"time_range": "本月"})

    assert lookup.value is None
    assert lookup.reason == "miss"


@pytest.mark.asyncio
async def test_get_hit_and_miss_are_tracked() -> None:
    cache, _ = make_cache()
    fingerprint = cache.scope_fingerprint(make_scope("scope-1"))
    params = {"time_range": "本月"}

    await cache.put("output", TENANT, fingerprint, params, b"rows")
    hit = await cache.get("output", TENANT, fingerprint, params)
    miss = await cache.get("output", TENANT, fingerprint, {"time_range": "上月"})

    assert hit.value == b"rows"
    assert hit.reason == "hit"
    assert miss.value is None
    assert miss.reason == "miss"
    assert cache.stats.hits["output"] == 1
    assert cache.stats.misses["output"] == 1


@pytest.mark.asyncio
async def test_store_unavailable_falls_back_to_source_of_truth() -> None:
    store = InMemoryCacheStore()
    cache, _ = make_cache(store)
    fingerprint = cache.scope_fingerprint(make_scope("scope-1"))
    params = {"time_range": "本月"}
    await cache.put("output", TENANT, fingerprint, params, b"rows")
    store.failure = RuntimeError("redis down")

    lookup = await cache.get("output", TENANT, fingerprint, params)

    assert lookup.value is None
    assert lookup.reason == "store_unavailable"
    # A put during an outage is swallowed, never raised.
    await cache.put("output", TENANT, fingerprint, params, b"rows")


@pytest.mark.asyncio
async def test_invalidate_scope_evicts_only_that_scope() -> None:
    cache, store = make_cache()
    fp_1 = cache.scope_fingerprint(make_scope("scope-1"))
    fp_2 = cache.scope_fingerprint(make_scope("scope-2"))
    await cache.put("output", TENANT, fp_1, {"t": "1"}, b"a")
    await cache.put("output", TENANT, fp_2, {"t": "1"}, b"b")

    await cache.invalidate_scope(TENANT, fp_1)

    # Only the scope-1 line is evicted; scope-2 survives as a different scope.
    assert (await cache.get("output", TENANT, fp_1, {"t": "1"})).value is None
    assert (await cache.get("output", TENANT, fp_2, {"t": "1"})).value == b"b"
    assert len(store.values) == 1


@pytest.mark.asyncio
async def test_purge_all_evicts_everything() -> None:
    cache, store = make_cache()
    fingerprint = cache.scope_fingerprint(make_scope("scope-1"))
    await cache.put("payroll", TENANT, fingerprint, {"t": "1"}, b"x")

    await cache.purge_all()

    assert store.values == {}


def test_unknown_domain_is_rejected() -> None:
    cache, _ = make_cache()
    with pytest.raises(UnknownCacheDomainError):
        cache.build_key("sql", TENANT, "fp", {})


def test_ttls_are_short_and_per_domain() -> None:
    cache, _ = make_cache()
    assert cache.ttl_seconds("identity_org") == 300
    assert cache.ttl_seconds("order_progress") == 120
    assert cache.ttl_seconds("output") == 60
    assert cache.ttl_seconds("payroll") == 60
