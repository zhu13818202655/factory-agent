"""Tenant lifecycle policy for factory questions (R1/R2/R3).

Two responsibilities, both of them one-way doors:

* **first-seen registration** — the customer MES credential exchange is the
  authority on which AppKeys exist, so the moment an exchange succeeds is the
  one moment this service learns of a new factory;
* **suspended-tenant admission** — a suspended factory must not be able to ask
  a question at all.

Both fail open. A registry read or a registration write that fails degrades to
"answer normally", never to "the whole factory is down", and never to silence:
each failure emits a structured log event and an alert.

The rejection itself is enforced by a router-level dependency in
``factory_agent.api.identity``; keeping the policy object free of FastAPI and of
the application container is what lets the container own it without an import
cycle.
"""

from dataclasses import dataclass

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.tenant_registry import TenantRegistryReader, TenantRegistryWriter
from factory_agent.statistics.alerts import AlertSink

_LOGGER = get_logger("factory_agent.api.tenant_lifecycle")

#: Stable front-end code for "this factory account is suspended".
TENANT_DISABLED_DETAIL = "tenant_disabled"

#: Registration modes (D-3). ``allowlist`` is reserved, not implemented.
REGISTRATION_MODE_AUTO = "auto"
REGISTRATION_MODE_ALLOWLIST = "allowlist"

#: Registry status meaning "every question from this factory is refused".
DISABLED_STATUS = "disabled"


@dataclass(frozen=True, slots=True)
class TenantLifecycle:
    """Registry-backed admission policy for factory questions."""

    reader: TenantRegistryReader | None = None
    writer: TenantRegistryWriter | None = None
    registration_mode: str = REGISTRATION_MODE_AUTO
    alerts: AlertSink | None = None

    async def is_disabled(self, tenant_id: str) -> bool:
        """Whether this tenant is suspended; an unreadable registry means "no"."""
        if self.reader is None:
            return False
        try:
            record = await self.reader.get(tenant_id)
        except Exception:  # noqa: BLE001 - a registry outage must not close the factory
            _LOGGER.exception("tenant.registry_read_failed")
            await self._alert("tenant.registry_read_failed")
            return False
        return record is not None and record.status == DISABLED_STATUS

    async def register_seen(self, tenant_id: str) -> str | None:
        """Register a first-seen factory; returns its ref, or ``None``.

        ``None`` covers the benign cases — no durable store, ``allowlist`` mode,
        the ordinary repeat visit — as well as a failed write, which is alerted
        and otherwise ignored.
        """
        if self.writer is None or self.registration_mode != REGISTRATION_MODE_AUTO:
            return None
        try:
            return await self.writer.register_seen(tenant_id)
        except Exception:  # noqa: BLE001 - a ledger write never blocks an answer
            _LOGGER.exception("tenant.registration_failed")
            await self._alert("tenant.registration_failed")
            return None

    async def _alert(self, kind: str) -> None:
        # Metadata only: the AppKey is the customer credential and never reaches
        # a log line, a trace, or an alert payload.
        if self.alerts is None:
            return
        try:
            await self.alerts.alert(kind, {"component": "tenant_lifecycle"})
        except Exception:  # noqa: BLE001 - alerting is best effort
            _LOGGER.exception("tenant.alert_failed")


__all__ = [
    "DISABLED_STATUS",
    "REGISTRATION_MODE_ALLOWLIST",
    "REGISTRATION_MODE_AUTO",
    "TENANT_DISABLED_DETAIL",
    "TenantLifecycle",
]
