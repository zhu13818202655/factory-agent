"""Tenant master data service: factory-account management (F2.1~F2.6).

This service owns ``tenant_registry`` and is the only writer of it outside the
first-seen registration path. Every write is admin-only (D14) and lands in
``admin_audit`` with the operator, the target, and the before/after values.
Deleting an account means disabling it (D10) — history is never physically
removed.

Tenants are addressed by ``tenant_ref``, not by AppKey. The AppKey never leaves
this service except once, in the create response, and a masked AppKey is not
unique across tenants; audit records therefore carry the ref.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from factory_agent.statistics.platform import PlatformScope, PlatformScopeError
from factory_agent.statistics.store import (
    AuditEntry,
    TenantRegistryRecord,
    UsageStore,
)

ACTIVE = "active"
DISABLED = "disabled"

#: Placeholder shown to operators until someone renames the factory. It derives
#: from the non-secret ref, never from the AppKey (which would put a secret
#: fragment into master data, listings, and audit) and never from the MES
#: user's name (personal data does not belong in tenant master data).
PLACEHOLDER_NAME_PREFIX = "未命名工厂-"


class TenantRegistryError(ValueError):
    """Structured rejection for an invalid or conflicting tenant record."""


@dataclass(frozen=True, slots=True)
class TenantRegistryPage:
    items: tuple[TenantRegistryRecord, ...]
    total: int
    next_cursor: int | None


def generate_app_key() -> str:
    """A platform-generated AppKey (R4: operation may also supply one)."""
    return f"fac-{secrets.token_hex(8)}"


def generate_tenant_ref() -> str:
    """A short, non-secret, publicly quotable handle for one tenant.

    It is not derived from the AppKey: an opaque random handle cannot be walked
    back into the secret, and it stays unique after masking would collide.
    """
    return f"t_{secrets.token_hex(4)}"


def placeholder_name(tenant_ref: str) -> str:
    return f"{PLACEHOLDER_NAME_PREFIX}{tenant_ref[:6]}"


class TenantRegistryService:
    def __init__(
        self,
        store: UsageStore,
        *,
        clock: Callable[[], datetime],
        new_id: Callable[[], str],
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id

    async def list(self, scope: PlatformScope, *, limit: int, offset: int) -> TenantRegistryPage:
        if offset < 0:
            raise TenantRegistryError("offset must not be negative")
        records, total = await self._store.list_tenant_registry(limit, offset)
        visible = [record for record in records if scope.covers_tenant(record.app_key)]
        # The cursor advances by rows actually scanned, not by rows returned: a
        # platform scope hides rows, so advancing by the visible count would
        # re-read or skip pages against the unscoped total.
        scanned = offset + len(records)
        next_cursor = scanned if scanned < total else None
        return TenantRegistryPage(tuple(visible), total, next_cursor)

    async def get(self, scope: PlatformScope, tenant_ref: str) -> TenantRegistryRecord | None:
        record = await self._store.get_tenant_registry_by_ref(tenant_ref)
        if record is None or not scope.covers_tenant(record.app_key):
            return None
        return record

    async def create(
        self,
        scope: PlatformScope,
        *,
        tenant_name: str,
        status: str,
        app_key: str | None = None,
    ) -> TenantRegistryRecord:
        self._require_admin(scope)
        name = tenant_name.strip()
        if not name:
            raise TenantRegistryError("tenant_name must not be empty")
        status_value = _validate_status(status)
        key = (app_key or "").strip() or generate_app_key()
        tenant_ref = generate_tenant_ref()
        now = self._clock()
        record = TenantRegistryRecord(
            app_key=key,
            tenant_ref=tenant_ref,
            tenant_name=name,
            status=status_value,
            created_at=now,
            updated_at=now,
        )
        created = await self._store.create_tenant_registry(record)
        if not created:
            raise TenantRegistryError("app_key already exists")
        await self._audit(
            scope,
            "tenant.create",
            tenant_ref,
            {"tenant_name": name, "status": status_value},
        )
        return record

    async def update(
        self,
        scope: PlatformScope,
        tenant_ref: str,
        *,
        tenant_name: str | None,
        status: str | None,
    ) -> TenantRegistryRecord:
        self._require_admin(scope)
        before = await self._store.get_tenant_registry_by_ref(tenant_ref)
        if before is None:
            raise TenantRegistryError("tenant not found")
        name = tenant_name.strip() if tenant_name is not None else None
        if name is not None and not name:
            raise TenantRegistryError("tenant_name must not be empty")
        status_value = _validate_status(status) if status is not None else None
        after = await self._store.update_tenant_registry(
            before.app_key,
            tenant_name=name,
            status=status_value,
            updated_at=self._clock(),
        )
        if after is None:
            raise TenantRegistryError("tenant not found")
        await self._audit(
            scope,
            "tenant.update",
            tenant_ref,
            {
                "before": {"tenant_name": before.tenant_name, "status": before.status},
                "after": {"tenant_name": after.tenant_name, "status": after.status},
            },
        )
        return after

    async def disable(self, scope: PlatformScope, tenant_ref: str) -> TenantRegistryRecord:
        """D10: deletion is a soft disable that preserves all history."""
        return await self._set_status(scope, tenant_ref, DISABLED, "tenant.disable")

    async def enable(self, scope: PlatformScope, tenant_ref: str) -> TenantRegistryRecord:
        return await self._set_status(scope, tenant_ref, ACTIVE, "tenant.enable")

    async def is_disabled(self, app_key: str) -> bool:
        """AppKey lookup for the business-edge guard; unknown means not disabled."""
        record = await self._store.get_tenant_registry(app_key)
        return record is not None and record.status == DISABLED

    async def _set_status(
        self, scope: PlatformScope, tenant_ref: str, status: str, action: str
    ) -> TenantRegistryRecord:
        self._require_admin(scope)
        before = await self._store.get_tenant_registry_by_ref(tenant_ref)
        if before is None:
            raise TenantRegistryError("tenant not found")
        after = await self._store.update_tenant_registry(
            before.app_key, tenant_name=None, status=status, updated_at=self._clock()
        )
        if after is None:
            raise TenantRegistryError("tenant not found")
        await self._audit(
            scope,
            action,
            tenant_ref,
            {
                "before": {"status": before.status},
                "after": {"status": after.status},
            },
        )
        return after

    def _require_admin(self, scope: PlatformScope) -> None:
        if not scope.allows_manage_tenants():
            raise PlatformScopeError("tenant management requires the admin role")

    async def _audit(
        self,
        scope: PlatformScope,
        action: str,
        target: str | None,
        detail: dict[str, object],
    ) -> None:
        await self._store.record_audit(
            AuditEntry(
                audit_id=self._new_id(),
                principal_id=scope.principal_id,
                action=action,
                target=target,
                detail=detail,
                created_at=self._clock(),
            )
        )


def _validate_status(status: str) -> str:
    if status not in (ACTIVE, DISABLED):
        raise TenantRegistryError(f"status must be 'active' or 'disabled', got {status!r}")
    return status


__all__ = [
    "ACTIVE",
    "DISABLED",
    "PLACEHOLDER_NAME_PREFIX",
    "TenantRegistryError",
    "TenantRegistryPage",
    "TenantRegistryService",
    "generate_app_key",
    "generate_tenant_ref",
    "placeholder_name",
]
