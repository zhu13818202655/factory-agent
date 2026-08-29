"""Tenant registry service tests (F2.1~F2.6, D10/D14)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from usage_admin.platform import PlatformRole, PlatformScope, PlatformScopeError
from usage_admin.store import InMemoryUsageStore
from usage_admin.tenants import (
    ACTIVE,
    DISABLED,
    TenantRegistryError,
    TenantRegistryService,
    generate_app_key,
)

NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)

ADMIN = PlatformScope("ops-1", PlatformRole.ADMIN, frozenset())
ANALYST = PlatformScope("ops-2", PlatformRole.ANALYST, frozenset())
SCOPED_ADMIN = PlatformScope("ops-3", PlatformRole.ADMIN, frozenset({"fac-01"}))


def make_service() -> tuple[TenantRegistryService, InMemoryUsageStore]:
    store = InMemoryUsageStore()
    counter = iter(range(1000))
    service = TenantRegistryService(
        store,
        clock=lambda: NOW,
        new_id=lambda: f"audit-{next(counter)}",
    )
    return service, store


@pytest.mark.asyncio
async def test_generate_app_key_produces_unique_keys() -> None:
    keys = {generate_app_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(key.startswith("fac-") for key in keys)


@pytest.mark.asyncio
async def test_create_requires_admin() -> None:
    service, _ = make_service()
    with pytest.raises(PlatformScopeError, match="admin"):
        await service.create(ANALYST, tenant_name="温州一厂", status=ACTIVE)


@pytest.mark.asyncio
async def test_create_returns_record_and_audits() -> None:
    service, store = make_service()

    record = await service.create(ADMIN, tenant_name="温州一厂", status=ACTIVE)

    assert record.app_key.startswith("fac-")
    assert record.status == ACTIVE
    assert any(entry.action == "tenant.create" for entry in store.audits)


@pytest.mark.asyncio
async def test_create_with_supplied_app_key_and_duplicate_rejected() -> None:
    service, _ = make_service()
    await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-01")

    with pytest.raises(TenantRegistryError, match="already exists"):
        await service.create(ADMIN, tenant_name="B", status=ACTIVE, app_key="fac-01")


@pytest.mark.asyncio
async def test_list_paginates_and_honours_scope() -> None:
    service, _ = make_service()
    await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-01")
    await service.create(ADMIN, tenant_name="B", status=ACTIVE, app_key="fac-02")

    page = await service.list(ADMIN, limit=1, offset=0)

    assert page.total == 2
    assert len(page.items) == 1
    assert page.next_cursor == 1
    scoped_page = await service.list(SCOPED_ADMIN, limit=10, offset=0)
    assert [item.app_key for item in scoped_page.items] == ["fac-01"]


@pytest.mark.asyncio
async def test_update_changes_name_and_status_with_before_after_audit() -> None:
    service, store = make_service()
    await service.create(ADMIN, tenant_name="旧名", status=ACTIVE, app_key="fac-01")

    updated = await service.update(ADMIN, "fac-01", tenant_name="新名", status=DISABLED)

    assert updated.tenant_name == "新名"
    assert updated.status == DISABLED
    entry = next(entry for entry in store.audits if entry.action == "tenant.update")
    assert entry.detail["before"] == {"tenant_name": "旧名", "status": "active"}
    assert entry.detail["after"] == {"tenant_name": "新名", "status": "disabled"}


@pytest.mark.asyncio
async def test_disable_is_soft_and_preserves_history() -> None:
    service, store = make_service()
    await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-01")

    disabled = await service.disable(ADMIN, "fac-01")

    assert disabled.status == DISABLED
    # No physical delete: the record still exists for billing reconciliation.
    record = await service.get(ADMIN, "fac-01")
    assert record is not None
    assert record.status == DISABLED
    assert any(entry.action == "tenant.disable" for entry in store.audits)


@pytest.mark.asyncio
async def test_enable_reactivates_after_disable() -> None:
    service, _ = make_service()
    await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-01")
    await service.disable(ADMIN, "fac-01")

    enabled = await service.enable(ADMIN, "fac-01")

    assert enabled.status == ACTIVE


@pytest.mark.asyncio
async def test_missing_tenant_raises_not_found() -> None:
    service, _ = make_service()
    with pytest.raises(TenantRegistryError, match="not found"):
        await service.disable(ADMIN, "fac-unknown")
    assert await service.get(ADMIN, "fac-unknown") is None


@pytest.mark.asyncio
async def test_invalid_status_and_empty_name_are_rejected() -> None:
    service, _ = make_service()
    with pytest.raises(TenantRegistryError, match="status"):
        await service.create(ADMIN, tenant_name="A", status="paused")
    with pytest.raises(TenantRegistryError, match="tenant_name"):
        await service.create(ADMIN, tenant_name="   ", status=ACTIVE)


@pytest.mark.asyncio
async def test_audit_targets_never_contain_plaintext_app_key() -> None:
    service, store = make_service()
    await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="super-secret-key-123")

    for entry in store.audits:
        assert entry.target is None or "super-secret-key-123" not in entry.target
        serialized = str(entry.detail)
        assert "super-secret-key-123" not in serialized
