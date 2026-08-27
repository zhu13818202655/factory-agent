from __future__ import annotations

from typing import Protocol


class CacheStore(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None:
        """Best-effort invalidation of every key under a prefix.

        Used when an organization, permission, contract, metric, or sensitive
        classification change must evict old cache lines (Story 2 scope_version
        consumers). Stores without prefix support should implement a no-op.
        """
        ...
