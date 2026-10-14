from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from factory_agent.domain import DataScope, TenantId

type DictionaryValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class DictionaryBatch:
    """A validated, tenant-bound safe directory projection.

    ``entity_ids`` describe a complete entity subset, never a complete tenant
    directory. The caller resolves bill identifiers from an authorized fact
    view before requesting bill configuration.
    """

    dataset_id: str
    rows: tuple[Mapping[str, DictionaryValue], ...]
    total: int
    complete: bool
    generated_at: datetime
    entity_ids: tuple[str, ...] = ()
    cache_source: Literal["cache", "live"] = "live"
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None:
            raise ValueError("directory generation time must be timezone-aware")
        if self.total < 0:
            raise ValueError("directory total must be nonnegative")
        if self.complete and len(self.rows) != self.total:
            raise ValueError("complete directory row count must match its total")
        object.__setattr__(self, "rows", tuple(MappingProxyType(dict(row)) for row in self.rows))


class DictionarySource(Protocol):
    async def fetch(
        self, dataset_id: str, scope: DataScope, entity_ids: tuple[str, ...] = ()
    ) -> DictionaryBatch: ...


class DictionaryCacheRead(Protocol):
    @property
    def value(self) -> bytes | None: ...


class DictionaryCache(Protocol):
    async def get(
        self,
        domain: str,
        tenant_id: TenantId,
        scope_fingerprint: str,
        params: Mapping[str, object],
    ) -> DictionaryCacheRead: ...

    async def put(
        self,
        domain: str,
        tenant_id: TenantId,
        scope_fingerprint: str,
        params: Mapping[str, object],
        value: bytes,
        *,
        ttl_seconds: int | None = None,
    ) -> bool: ...
