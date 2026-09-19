"""Tenant registry service tests (F2.1~F2.6, D10/D14, D-4)."""

from datetime import datetime, timezone

import pytest

from factory_agent.statistics.platform import PlatformRole, PlatformScope, PlatformScopeError
from factory_agent.statistics.store import InMemoryUsageStore
from factory_agent.statistics.tenants import (
    ACTIVE,
    DISABLED,
    TenantRegistryError,
    TenantRegistryService,
    generate_app_key,
    generate_tenant_ref,
    placeholder_name,
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


def test_generate_tenant_ref_is_short_and_non_secret() -> None:
    refs = {generate_tenant_ref() for _ in range(50)}
    assert len(refs) == 50
    assert all(ref.startswith("t_") and len(ref) == 10 for ref in refs)
    assert placeholder_name("t_7f3a9c21") == "未命名工厂-t_7f3a"


@pytest.mark.asyncio
async def test_create_requires_admin() -> None:
    service, _ = make_service()
    with pytest.raises(PlatformScopeError, match="admin"):
        await service.create(ANALYST, tenant_name="温州一厂", status=ACTIVE)


@pytest.mark.asyncio
async def test_create_returns_record_with_ref_and_audits() -> None:
    service, store = make_service()

    record = await service.create(ADMIN, tenant_name="温州一厂", status=ACTIVE)

    assert record.app_key.startswith("fac-")
    assert record.tenant_ref.startswith("t_")
    assert record.status == ACTIVE
    entry = next(entry for entry in store.audits if entry.action == "tenant.create")
    # D-4: audit carries the non-secret ref, never the AppKey or a masked key.
    assert entry.target == record.tenant_ref


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
async def test_list_cursor_advances_by_rows_scanned_not_rows_returned() -> None:
    """A platform scope hides rows; the cursor must still walk the whole page set.

    Advancing by the visible count would re-read the hidden rows forever.
    """
    service, _ = make_service()
    await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-99")
    await service.create(ADMIN, tenant_name="B", status=ACTIVE, app_key="fac-01")

    first = await service.list(SCOPED_ADMIN, limit=1, offset=0)
    assert first.items == ()
    assert first.next_cursor == 1

    second = await service.list(SCOPED_ADMIN, limit=1, offset=1)
    assert [item.app_key for item in second.items] == ["fac-01"]
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_update_changes_name_and_status_with_before_after_audit() -> None:
    service, store = make_service()
    created = await service.create(ADMIN, tenant_name="旧名", status=ACTIVE, app_key="fac-01")

    updated = await service.update(ADMIN, created.tenant_ref, tenant_name="新名", status=DISABLED)

    assert updated.tenant_name == "新名"
    assert updated.status == DISABLED
    entry = next(entry for entry in store.audits if entry.action == "tenant.update")
    assert entry.detail["before"] == {"tenant_name": "旧名", "status": "active"}
    assert entry.detail["after"] == {"tenant_name": "新名", "status": "disabled"}
    assert entry.target == created.tenant_ref


@pytest.mark.asyncio
async def test_disable_is_soft_and_preserves_history() -> None:
    service, store = make_service()
    created = await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-01")

    disabled = await service.disable(ADMIN, created.tenant_ref)

    assert disabled.status == DISABLED
    # No physical delete: the record still exists for billing reconciliation.
    record = await service.get(ADMIN, created.tenant_ref)
    assert record is not None
    assert record.status == DISABLED
    assert await service.is_disabled("fac-01") is True
    assert any(entry.action == "tenant.disable" for entry in store.audits)


@pytest.mark.asyncio
async def test_enable_reactivates_after_disable() -> None:
    service, _ = make_service()
    created = await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-01")
    await service.disable(ADMIN, created.tenant_ref)

    enabled = await service.enable(ADMIN, created.tenant_ref)

    assert enabled.status == ACTIVE


@pytest.mark.asyncio
async def test_unknown_tenant_is_never_disabled() -> None:
    """An unknown AppKey is not a disabled one: clearing the ledger locks nobody out."""
    service, _ = make_service()
    assert await service.is_disabled("fac-never-seen") is False


@pytest.mark.asyncio
async def test_missing_tenant_raises_not_found() -> None:
    service, _ = make_service()
    with pytest.raises(TenantRegistryError, match="not found"):
        await service.disable(ADMIN, "t_deadbeef")
    assert await service.get(ADMIN, "t_deadbeef") is None


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


@pytest.mark.asyncio
async def test_audit_targets_distinguish_tenants_sharing_a_masked_prefix() -> None:
    """The old masked target made two tenants indistinguishable in the audit log."""
    from factory_agent.statistics.masking import mask_app_key

    service, store = make_service()
    first = await service.create(ADMIN, tenant_name="A", status=ACTIVE, app_key="fac-0123456789")
    second = await service.create(ADMIN, tenant_name="B", status=ACTIVE, app_key="fac-0199999999")

    assert mask_app_key("fac-0123456789") == mask_app_key("fac-0199999999")
    targets = {entry.target for entry in store.audits}
    assert first.tenant_ref in targets
    assert second.tenant_ref in targets
    assert len(targets) == 2
