"""Authorization-aware cache with scope fingerprints and versioned keys.

Redis is only an optimization. Every business key carries the tenant, an
irreversible scope fingerprint (consuming ``DataScope.scope_version``), the
canonical parameters, and the contract/metric/data versions. A cache line is
returned only when the caller can prove the exact same scope; on store errors,
unknown versions, or unprovable scopes the cache falls back to the source of
truth.

Base-data exception (Story 1): the master-data interfaces (employee /
department / huohao …) return the full roster regardless of the calling role
(customer confirmation 4), so their cache lines live in the ``identity_org``
domain under a single shared fingerprint — no scope fingerprint in the key.
Any role's first query populates the shared line and every later role reuses
it; no super-account or dedicated channel is required. Bumping the data
version (Mock rebuild / master-data change) or calling
``invalidate_base_data`` evicts the shared lines.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from factory_agent.domain import DataScope, TenantId
from factory_agent.ports import CacheStore

#: Cache domains and their TTLs (seconds). Business domains stay short; the
#: base-data ``identity_org`` domain refreshes daily because master data is
#: role-independent and changes rarely. TTLs are conservative placeholders to
#: be confirmed by real-data review; performance is not a gate.
CACHE_DOMAINS: tuple[str, ...] = ("identity_org", "order_progress", "output", "payroll")

DEFAULT_TTL_BY_DOMAIN: dict[str, int] = {
    "identity_org": 86400,
    "order_progress": 120,
    "output": 60,
    "payroll": 60,
}

#: Domains whose data is role-independent full-roster master data. Their keys
#: use a shared fingerprint instead of a scope fingerprint.
SCOPELESS_DOMAINS: frozenset[str] = frozenset({"identity_org"})

#: Fingerprint used for scope-free base-data cache lines.
SHARED_SCOPE_FINGERPRINT = "shared"

KEY_PREFIX = "fa"


class UnknownCacheDomainError(ValueError):
    """Raised when a cache domain is not in the reviewed registry."""


@dataclass(frozen=True, slots=True)
class CachePolicy:
    ttl_seconds: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TTL_BY_DOMAIN))


@dataclass(frozen=True, slots=True)
class CacheLookup:
    value: bytes | None
    reason: str


@dataclass
class CacheStats:
    """Hit/miss and fallback-reason bookkeeping for observability and tests."""

    hits: Counter[str] = field(default_factory=lambda: Counter[str]())
    misses: Counter[str] = field(default_factory=lambda: Counter[str]())
    reasons: Counter[str] = field(default_factory=lambda: Counter[str]())

    def record_hit(self, domain: str) -> None:
        self.hits[domain] += 1

    def record_miss(self, domain: str, reason: str) -> None:
        self.misses[domain] += 1
        self.reasons[reason] += 1


class AuthAwareCache:
    """Versioned, scope-bound cache with source-of-truth fallback."""

    def __init__(
        self,
        store: CacheStore,
        *,
        contract_version: str,
        metric_version: str,
        data_version: str,
        policy: CachePolicy | None = None,
        stats: CacheStats | None = None,
    ) -> None:
        self._store = store
        self._contract_version = contract_version
        self._metric_version = metric_version
        self._data_version = data_version
        self._policy = policy or CachePolicy()
        self._stats = stats or CacheStats()

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def scope_fingerprint(self, scope: DataScope) -> str:
        """Irreversible summary of the effective scope.

        Consumes ``DataScope.scope_version`` and records only counts — never the
        ``employee_ids``/``dept_ids`` values themselves.
        """
        summary = (
            f"{scope.scope_version}\x1f{scope.tenant_id}\x1f{scope.mes_filtered}\x1f"
            f"{len(scope.employee_ids)}\x1f{len(scope.dept_ids)}"
        )
        return hashlib.sha256(summary.encode()).hexdigest()

    def build_key(
        self,
        domain: str,
        tenant_id: TenantId,
        scope_fingerprint: str,
        params: Mapping[str, object],
    ) -> str:
        self._require_domain(domain)
        params_digest = _canonical_digest(params)
        return (
            f"{KEY_PREFIX}:{self._contract_version}:{self._metric_version}:"
            f"{self._data_version}:{tenant_id}:{scope_fingerprint}:{domain}:{params_digest}"
        )

    def ttl_seconds(self, domain: str) -> int:
        self._require_domain(domain)
        return self._policy.ttl_seconds.get(domain, DEFAULT_TTL_BY_DOMAIN[domain])

    async def get(
        self,
        domain: str,
        tenant_id: TenantId,
        scope_fingerprint: str,
        params: Mapping[str, object],
    ) -> CacheLookup:
        """Return the cached value only when the scope fingerprint matches.

        A store error, an unknown domain, or a miss all fall back to the source
        of truth — never a stale or unscoped detail row.
        """
        if domain not in CACHE_DOMAINS:
            raise UnknownCacheDomainError(f"unknown cache domain {domain!r}")
        key = self.build_key(domain, tenant_id, scope_fingerprint, params)
        try:
            value = await self._store.get(key)
        except Exception:  # noqa: BLE001 - Redis is an optimization, never a gate
            self._stats.record_miss(domain, "store_unavailable")
            return CacheLookup(None, "store_unavailable")
        if value is None:
            self._stats.record_miss(domain, "miss")
            return CacheLookup(None, "miss")
        self._stats.record_hit(domain)
        return CacheLookup(value, "hit")

    async def put(
        self,
        domain: str,
        tenant_id: TenantId,
        scope_fingerprint: str,
        params: Mapping[str, object],
        value: bytes,
    ) -> None:
        """Best-effort write; a store failure never propagates to the caller."""
        if domain not in CACHE_DOMAINS:
            raise UnknownCacheDomainError(f"unknown cache domain {domain!r}")
        key = self.build_key(domain, tenant_id, scope_fingerprint, params)
        try:
            await self._store.put(key, value, self.ttl_seconds(domain))
        except Exception:  # noqa: BLE001
            self._stats.record_miss(domain, "store_unavailable")

    async def invalidate_scope(self, tenant_id: TenantId, scope_fingerprint: str) -> None:
        """Evict every cache line bound to one scope (org/permission changes)."""
        prefix = (
            f"{KEY_PREFIX}:{self._contract_version}:{self._metric_version}:"
            f"{self._data_version}:{tenant_id}:{scope_fingerprint}:"
        )
        try:
            await self._store.delete_prefix(prefix)
        except Exception:  # noqa: BLE001
            self._stats.record_miss("identity_org", "store_unavailable")

    async def invalidate_base_data(self, tenant_id: TenantId) -> None:
        """Manual invalidation entry for the shared base-data lines.

        Called after a Mock PG rebuild or a customer master-data change so the
        next query re-fetches the full roster instead of a stale headcount or
        department structure. Only the scope-free ``identity_org`` lines are
        evicted; business caches keep their own short TTLs.
        """
        await self.invalidate_scope(tenant_id, SHARED_SCOPE_FINGERPRINT)

    async def purge_all(self) -> None:
        """Evict every cache line (contract/metric/sensitive classification change)."""
        try:
            await self._store.delete_prefix(f"{KEY_PREFIX}:")
        except Exception:  # noqa: BLE001
            self._stats.record_miss("identity_org", "store_unavailable")

    def _require_domain(self, domain: str) -> None:
        if domain not in CACHE_DOMAINS:
            raise UnknownCacheDomainError(f"unknown cache domain {domain!r}")


class CachedDirectorySource:
    """Scope-free base-data cache in front of a directory source.

    Implements both the ``DirectoryResolver`` and ``OrganizationSource`` ports.
    The full-roster department/employee lookups (role-independent per customer
    confirmation 4) are cached in the ``identity_org`` domain under the shared
    fingerprint, so whichever role queries first populates the line and every
    later role reuses it without re-fetching. Per-employee current-dept
    resolution is role-specific and is never cached here. Every cache failure
    falls back to the wrapped source.
    """

    def __init__(self, inner: Any, cache: AuthAwareCache) -> None:
        self._inner = inner
        self._cache = cache

    async def list_depts(self, scope: DataScope) -> tuple[Any, ...]:
        return await self._cached_roster(
            scope.tenant_id, "depts", lambda: self._inner.list_depts(scope)
        )

    async def list_employees(self, scope: DataScope) -> tuple[Any, ...]:
        return await self._cached_roster(
            scope.tenant_id, "employees", lambda: self._inner.list_employees(scope)
        )

    async def list_current_depts(self, tenant_id: TenantId, employee_id: Any) -> tuple[Any, ...]:
        return await self._inner.list_current_depts(tenant_id, employee_id)

    async def _cached_roster(self, tenant_id: TenantId, op: str, fetch: Any) -> tuple[Any, ...]:
        params = {"op": op}
        lookup = await self._cache.get("identity_org", tenant_id, SHARED_SCOPE_FINGERPRINT, params)
        if lookup.value is not None:
            records = _decode_records(lookup.value)
            if records is not None:
                return records
        records = await fetch()
        await self._cache.put(
            "identity_org", tenant_id, SHARED_SCOPE_FINGERPRINT, params, _encode_records(records)
        )
        return records


def _encode_records(records: Any) -> bytes:
    payload = [asdict(record) for record in records]
    return json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")


def _decode_records(raw: bytes) -> tuple[Any, ...] | None:
    """Rebuild cached directory records without importing vendor shapes.

    Returns ``None`` on any decode problem so the caller falls back to the
    source of truth instead of surfacing a corrupt line.
    """
    from factory_agent.ports.directory import DeptRecord, EmployeeRecord

    try:
        payload: object = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            return None
        entries = cast("list[object]", payload)
        records: list[DeptRecord | EmployeeRecord] = []
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            item = cast(dict[str, str], entry)
            if "dept_id" in item:
                records.append(
                    DeptRecord(dept_id=item["dept_id"], name=item["name"], name_pk=item["name_pk"])
                )
            elif "employee_id" in item:
                records.append(
                    EmployeeRecord(
                        employee_id=item["employee_id"],
                        name=item["name"],
                        name_pk=item["name_pk"],
                    )
                )
            else:
                return None
        return tuple(records)
    except Exception:  # noqa: BLE001 - corrupt cache falls back to source
        return None


def _canonical_digest(params: Mapping[str, object]) -> str:
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    return repr(value)


__all__ = [
    "CACHE_DOMAINS",
    "AuthAwareCache",
    "CachedDirectorySource",
    "CacheLookup",
    "CachePolicy",
    "CacheStats",
    "DEFAULT_TTL_BY_DOMAIN",
    "SCOPELESS_DOMAINS",
    "SHARED_SCOPE_FINGERPRINT",
    "UnknownCacheDomainError",
]
