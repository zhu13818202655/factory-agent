from typing import Protocol, runtime_checkable


class CacheStore(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None:
        """Best-effort invalidation of every key under a prefix.

        Used when an organization, permission, contract, metric, or sensitive
        classification change must evict old cache lines (scope-version
        consumers). Stores without prefix support should implement a no-op.
        """
        ...


@runtime_checkable
class CacheLeaseStore(Protocol):
    async def acquire_lease(self, key: str, token: str, ttl_seconds: int) -> bool: ...

    async def release_lease(self, key: str, token: str) -> None: ...
