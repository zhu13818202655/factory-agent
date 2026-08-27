"""Authorization-aware cache with scope fingerprints and versioned keys.

Redis is only an optimization. Every key carries the tenant, an irreversible
scope fingerprint (consuming Story 2's ``scope_version``), the canonical
parameters, and the contract/metric/data versions. A cache line is returned
only when the caller can prove the exact same scope; on store errors, unknown
versions, or unprovable scopes the cache falls back to the source of truth.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from factory_agent.domain import DataScope, TenantId
from factory_agent.ports import CacheStore

#: Cache domains and their short TTLs (seconds). All TTLs are placeholders to
#: be confirmed by load testing in Story 9 (K3 — performance is not a gate in
#: this story).
CACHE_DOMAINS: tuple[str, ...] = ("identity_org", "order_progress", "output", "payroll")

DEFAULT_TTL_BY_DOMAIN: dict[str, int] = {
    "identity_org": 300,
    "order_progress": 120,
    "output": 60,
    "payroll": 60,
}

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

        Consumes Story 2's ``scope_version`` and records only counts — never the
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

    async def purge_all(self) -> None:
        """Evict every cache line (contract/metric/sensitive classification change)."""
        try:
            await self._store.delete_prefix(f"{KEY_PREFIX}:")
        except Exception:  # noqa: BLE001
            self._stats.record_miss("identity_org", "store_unavailable")

    def _require_domain(self, domain: str) -> None:
        if domain not in CACHE_DOMAINS:
            raise UnknownCacheDomainError(f"unknown cache domain {domain!r}")


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
    "CacheLookup",
    "CachePolicy",
    "CacheStats",
    "DEFAULT_TTL_BY_DOMAIN",
    "UnknownCacheDomainError",
]
