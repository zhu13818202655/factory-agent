"""In-memory cache store with optional fault injection for tests."""

from dataclasses import dataclass, field


@dataclass
class InMemoryCacheStore:
    values: dict[str, bytes] = field(default_factory=dict[str, bytes])
    ttl: dict[str, int] = field(default_factory=dict[str, int])
    failure: Exception | None = None
    leases: dict[str, str] = field(default_factory=dict[str, str])

    async def get(self, key: str) -> bytes | None:
        if self.failure is not None:
            raise self.failure
        return self.values.get(key)

    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
        if self.failure is not None:
            raise self.failure
        self.values[key] = value
        self.ttl[key] = ttl_seconds

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttl.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        if self.failure is not None:
            raise self.failure
        for key in [key for key in self.values if key.startswith(prefix)]:
            del self.values[key]
            self.ttl.pop(key, None)

    async def acquire_lease(self, key: str, token: str, ttl_seconds: int) -> bool:
        if self.failure is not None:
            raise self.failure
        if key in self.leases:
            return False
        self.leases[key] = token
        return True

    async def release_lease(self, key: str, token: str) -> None:
        if self.leases.get(key) == token:
            self.leases.pop(key)


__all__ = ["InMemoryCacheStore"]
