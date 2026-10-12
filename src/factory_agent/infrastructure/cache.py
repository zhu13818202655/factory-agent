"""Redis-backed cache store.

Redis is only an optimization: when it is unavailable every operation falls
back to the source of truth, and a cache line whose scope cannot be proven is
never returned. The application depends on the ``CacheStore`` protocol, never
on this concrete client.
"""

from typing import Any

import redis.asyncio as aioredis  # type: ignore[reportMissingTypeStubs]


class RedisCacheStore:
    """``CacheStore`` over Redis, with prefix invalidation via SCAN + DEL.

    Sockets are bounded (connect + command timeout): the cache sits on the
    request path, so a hung Redis must degrade to the source of truth within
    about a second instead of stalling the answer. A slow-but-alive Redis is
    no better than a dead one for an optimization layer.
    """

    def __init__(
        self,
        url: str,
        *,
        connect_timeout_seconds: float = 1.0,
        timeout_seconds: float = 1.0,
    ) -> None:
        self._client: Any = aioredis.from_url(  # type: ignore[reportUnknownMemberType]
            url,
            socket_connect_timeout=connect_timeout_seconds,
            socket_timeout=timeout_seconds,
        )

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

    async def acquire_lease(self, key: str, token: str, ttl_seconds: int) -> bool:
        return bool(await self._client.set(key, token, nx=True, ex=ttl_seconds))

    async def release_lease(self, key: str, token: str) -> None:
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        await self._client.eval(script, 1, key, token)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["RedisCacheStore"]
