"""Read-only tenant registry port.

``tenant_registry`` is owned and written by usage-admin; this service only ever
reads it. The port keeps the read behind a protocol so tests can inject a fake
and the SQL implementation stays isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TenantRegistryRecord:
    app_key: str
    tenant_name: str
    status: str


class TenantRegistryReader(Protocol):
    """Read-only view of the usage-admin-owned tenant registry."""

    async def get(self, app_key: str) -> TenantRegistryRecord | None:
        """Return the registry record, or ``None`` when the AppKey is unknown."""
        ...
