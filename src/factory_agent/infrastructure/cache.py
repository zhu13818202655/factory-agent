"""Redis-backed cache store.

Redis is only an optimization: when it is unavailable every operation falls
back to the source of truth, and a cache line whose scope cannot be proven is
never returned. The application depends on the ``CacheStore`` protocol, never
on this concrete client.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis  # type: ignore[reportMissingTypeStubs]


class RedisCacheStore:
    """``CacheStore`` over Redis, with prefix invalidation via SCAN + DEL."""

    def __init__(self, url: str) -> None:
        self._client: Any = aioredis.from_url(url)  # type: ignore[reportUnknownMemberType]

    async def get(self, key: str) -> bytes | None:
        value = await self._client.get(key)
        return value if isinstance(value, bytes) else None

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
        await self._client.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def delete_prefix(self, prefix: str) -> None:
        async for key in self._client.scan_iter(match=f"{prefix}*"):
            await self._client.delete(key)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["RedisCacheStore"]
