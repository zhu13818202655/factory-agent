"""Tenant registry ports.

``tenant_registry`` is owned by the statistics surface: it is the ledger of
which factories the platform has seen, and its ``status`` column is what the
business edge consults. The read port keeps that lookup behind a protocol so
tests can inject a fake, and the write port keeps first-seen registration
behind the same boundary so the API edge never imports the statistics store.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TenantRegistryRecord:
    app_key: str
    tenant_name: str
    status: str


class TenantRegistryReader(Protocol):
    """Read-only view of the tenant registry, keyed by AppKey."""

    async def get(self, app_key: str) -> TenantRegistryRecord | None:
        """Return the registry record, or ``None`` when the AppKey is unknown."""
        ...


class TenantRegistryWriter(Protocol):
    """Idempotent first-seen registration of an AppKey."""

    async def register_seen(self, app_key: str) -> str | None:
        """Register a first-seen AppKey and return its ``tenant_ref``.

        Returns ``None`` when the AppKey was already registered. Raises when the
        write itself fails; callers must fail open and alert rather than block
        the question — the customer MES token exchange, not this ledger, is the
        authority on whether an AppKey is real.
        """
        ...
