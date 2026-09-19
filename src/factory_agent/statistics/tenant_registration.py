"""First-seen AppKey registration (R1).

The customer MES credential exchange is the authority on which AppKeys exist:
an AppKey the customer accepts belongs to a real factory. This writer keeps the
local ledger in step by inserting the row the first time such an AppKey is seen
and doing nothing afterwards.

Two invariants shape the implementation:

* the placeholder name must not leak the secret — it derives from the
  non-secret ``tenant_ref``, never from the AppKey or from a person's name;
* a failed write must never block a question. The caller alerts and continues;
  the next question retries the upsert.
"""

from collections.abc import Callable
from datetime import datetime, timezone

from factory_agent.statistics.store import PostgresUsageStore
from factory_agent.statistics.tenants import generate_tenant_ref, placeholder_name


class StatisticsTenantRegistryWriter:
    """Registers a first-seen AppKey into ``tenant_registry`` idempotently."""

    def __init__(
        self,
        store: PostgresUsageStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def register_seen(self, app_key: str) -> str | None:
        """Insert the tenant row if this AppKey is new; return its ref or ``None``.

        A returned ref means this call created the row. ``None`` means the
        AppKey was already registered, which is the common case and costs one
        conflict-checked insert.
        """
        tenant_ref = generate_tenant_ref()
        created = await self._store.register_seen_tenant(
            app_key=app_key,
            tenant_ref=tenant_ref,
            tenant_name=placeholder_name(tenant_ref),
            at=self._clock(),
        )
        return tenant_ref if created else None


__all__ = ["StatisticsTenantRegistryWriter"]
