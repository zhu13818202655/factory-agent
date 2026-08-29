"""Tenant master data service: factory-account management (F2.1~F2.6).

This service owns ``tenant_registry`` (DDL + CRUD); factory-agent reads it
read-only. Every write is admin-only (D14) and lands in ``admin_audit`` with
the operator, the target, and the before/after values. Deleting an account
means disabling it (D10) — history is never physically removed.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from usage_admin.masking import mask_app_key
from usage_admin.platform import PlatformScope, PlatformScopeError
from usage_admin.store import (
    AuditEntry,
    TenantRegistryRecord,
    UsageStore,
)

ACTIVE = "active"
DISABLED = "disabled"


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
        next_cursor = offset + len(visible) if offset + len(visible) < total else None
        return TenantRegistryPage(tuple(visible), total, next_cursor)

    async def get(self, scope: PlatformScope, app_key: str) -> TenantRegistryRecord | None:
        record = await self._store.get_tenant_registry(app_key)
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
        now = self._clock()
        record = TenantRegistryRecord(
            app_key=key,
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
            _masked(key),
            {"tenant_name": name, "status": status_value},
        )
        return record

    async def update(
        self,
        scope: PlatformScope,
        app_key: str,
        *,
        tenant_name: str | None,
        status: str | None,
    ) -> TenantRegistryRecord:
        self._require_admin(scope)
        before = await self._store.get_tenant_registry(app_key)
        if before is None:
            raise TenantRegistryError("tenant not found")
        name = tenant_name.strip() if tenant_name is not None else None
        if name is not None and not name:
            raise TenantRegistryError("tenant_name must not be empty")
        status_value = _validate_status(status) if status is not None else None
        after = await self._store.update_tenant_registry(
            app_key,
            tenant_name=name,
            status=status_value,
            updated_at=self._clock(),
        )
        if after is None:
            raise TenantRegistryError("tenant not found")
        await self._audit(
            scope,
            "tenant.update",
            _masked(app_key),
            {
                "before": {"tenant_name": before.tenant_name, "status": before.status},
                "after": {"tenant_name": after.tenant_name, "status": after.status},
            },
        )
        return after

    async def disable(self, scope: PlatformScope, app_key: str) -> TenantRegistryRecord:
        """D10: deletion is a soft disable that preserves all history."""
        return await self._set_status(scope, app_key, DISABLED, "tenant.disable")

    async def enable(self, scope: PlatformScope, app_key: str) -> TenantRegistryRecord:
        return await self._set_status(scope, app_key, ACTIVE, "tenant.enable")

    async def _set_status(
        self, scope: PlatformScope, app_key: str, status: str, action: str
    ) -> TenantRegistryRecord:
        self._require_admin(scope)
        before = await self._store.get_tenant_registry(app_key)
        if before is None:
            raise TenantRegistryError("tenant not found")
        after = await self._store.update_tenant_registry(
            app_key, tenant_name=None, status=status, updated_at=self._clock()
        )
        if after is None:
            raise TenantRegistryError("tenant not found")
        await self._audit(
            scope,
            action,
            _masked(app_key),
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


def _masked(app_key: str) -> str:
    """Audit targets carry the masked key only (D9: no plaintext AppKey)."""
    masked = mask_app_key(app_key)
    return masked if masked is not None else ""


__all__ = [
    "ACTIVE",
    "DISABLED",
    "TenantRegistryError",
    "TenantRegistryPage",
    "TenantRegistryService",
    "generate_app_key",
]
