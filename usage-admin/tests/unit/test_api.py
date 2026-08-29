"""API-level tests for the ingest and admin endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr
from support.events import interaction_started
from usage_admin.api.server import create_app
from usage_admin.config import UsageAdminSettings
from usage_admin.container import build_container
from usage_admin.events import MesCallFact
from usage_admin.ingest import IngestService
from usage_admin.platform import PRINCIPAL_HEADER, ROLE_HEADER
from usage_admin.rollup import RollupEngine
from usage_admin.store import InMemoryUsageStore, TenantRegistryRecord

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def make_app(
    *,
    api_key: str | None = None,
    api_token: str | None = None,
    token_signing_secret: str | None = None,
):
    store = InMemoryUsageStore()
    settings = UsageAdminSettings(
        database_url=None,
        ingest_api_key=SecretStr(api_key) if api_key else None,
        export_signing_secret=SecretStr("test-secret"),
        download_base_url="http://usage-admin.test",
        api_token=SecretStr(api_token) if api_token else None,
        token_signing_secret=SecretStr(token_signing_secret) if token_signing_secret else None,
    )
    container = build_container(
        settings,
        store=store,
        clock=lambda: NOW,
        new_id=lambda: "export-1",
    )
    return create_app(settings, container=container), store


@pytest.mark.asyncio
async def test_ingest_endpoint_accepts_and_reports_outcomes() -> None:
    app, store = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/usage-events:batch",
            json={"events": [interaction_started("e-1"), interaction_started("e-1")]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == ["e-1"]
    assert body["duplicate"] == ["e-1"]
    assert len(store.raw_events) == 1


@pytest.mark.asyncio
async def test_ingest_requires_bearer_key_when_configured() -> None:
    app, _ = make_app(api_key="secret-key")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/usage-events:batch",
            json={"events": [interaction_started("e-1")]},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_endpoints_require_platform_headers() -> None:
    app, _ = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/v1/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_summary_endpoint_returns_metrics() -> None:
    app, store = make_app()
    await IngestService(store, clock=lambda: NOW).ingest(
        [interaction_started("s-1", user_subject_id="u" * 64)]
    )
    await RollupEngine(store, clock=lambda: NOW).rollup_range(frozenset({"tenant-a"}), START, END)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/v1/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers={PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "analyst"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["questions"] == 1
    assert body["metric_version"].startswith("rollup=")
    assert body["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_exports_require_analyst_role() -> None:
    app, _ = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/v1/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers={PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "viewer"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_export_download_roundtrip() -> None:
    app, store = make_app()
    await IngestService(store, clock=lambda: NOW).ingest([interaction_started("s-1")])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/v1/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers={PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "analyst"},
        )
        assert created.status_code == 201
        url = created.json()["download_url"]
        download = await client.get(url)

    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert download.content.startswith(b"users,")


@pytest.mark.asyncio
async def test_over_span_query_is_rejected_with_422() -> None:
    app, _ = make_app()
    wide_start = END - timedelta(days=400)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/v1/usage/summary",
            params={"start": wide_start.isoformat(), "end": END.isoformat()},
            headers={PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "analyst"},
        )

    assert response.status_code == 422


def _admin_headers() -> dict[str, str]:
    return {PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "admin"}


def _analyst_headers() -> dict[str, str]:
    return {PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "analyst"}


def _viewer_headers() -> dict[str, str]:
    return {PRINCIPAL_HEADER: "ops-1", ROLE_HEADER: "viewer"}


@pytest.mark.asyncio
async def test_registry_crud_requires_admin_for_writes() -> None:
    app, _ = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # analyst may read the list but not create.
        listed = await client.get("/admin/v1/tenants/registry", headers=_analyst_headers())
        assert listed.status_code == 200
        created = await client.post(
            "/admin/v1/tenants/registry",
            json={"tenant_name": "温州一厂", "status": "active"},
            headers=_analyst_headers(),
        )
        assert created.status_code == 403
        # admin creates.
        created = await client.post(
            "/admin/v1/tenants/registry",
            json={"tenant_name": "温州一厂", "status": "active"},
            headers=_admin_headers(),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["tenant_name"] == "温州一厂"
        assert body["status"] == "active"
        # create response carries the plaintext AppKey exactly once (D9).
        plaintext_key = body["app_key"]
        assert plaintext_key.startswith("fac-")
        assert "***" not in plaintext_key
        # every read response masks it.
        detail = await client.get(
            f"/admin/v1/tenants/registry/{plaintext_key}", headers=_admin_headers()
        )
        assert detail.status_code == 200
        assert detail.json()["app_key"] == f"{plaintext_key[:6]}***"


@pytest.mark.asyncio
async def test_registry_delete_is_disable_and_enable_reactivates() -> None:
    app, store = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/v1/tenants/registry",
            json={"tenant_name": "A", "status": "active"},
            headers=_admin_headers(),
        )
        app_key = created.json()["app_key"]
        deleted = await client.delete(
            f"/admin/v1/tenants/registry/{app_key}", headers=_admin_headers()
        )
        assert deleted.status_code == 204
        detail = await client.get(f"/admin/v1/tenants/registry/{app_key}", headers=_admin_headers())
        assert detail.json()["status"] == "disabled"
        # history is preserved: the record still exists.
        assert store.tenant_registry[app_key].status == "disabled"
        enabled = await client.post(
            f"/admin/v1/tenants/registry/{app_key}/enable", headers=_admin_headers()
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"] == "active"


@pytest.mark.asyncio
async def test_auth_register_login_and_bearer_flow() -> None:
    app, _ = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # non-admin cannot register.
        denied = await client.post(
            "/admin/v1/auth/register",
            json={"username": "ops", "password": "password-123", "role": "viewer"},
            headers=_analyst_headers(),
        )
        assert denied.status_code == 403
        registered = await client.post(
            "/admin/v1/auth/register",
            json={"username": "ops", "password": "password-123", "role": "admin"},
            headers=_admin_headers(),
        )
        assert registered.status_code == 201
        assert registered.json()["role"] == "admin"
        logged = await client.post(
            "/admin/v1/auth/login",
            json={"username": "ops", "password": "password-123"},
        )
        assert logged.status_code == 200
        token = logged.json()["token"]
        # bearer token authenticates the same admin scope.
        summary = await client.get(
            "/admin/v1/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert summary.status_code == 200
        # a bad password never yields a token.
        failed = await client.post(
            "/admin/v1/auth/login",
            json={"username": "ops", "password": "wrong-password"},
        )
        assert failed.status_code == 401


@pytest.mark.asyncio
async def test_frontend_api_token_bearer_channel() -> None:
    app, _ = make_app(api_token="frontend-token-abc")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/v1/tenants/registry",
            json={"tenant_name": "前端工厂", "status": "active"},
            headers={"Authorization": "Bearer frontend-token-abc"},
        )
        assert created.status_code == 201
        # a tampered token is rejected with 403.
        denied = await client.post(
            "/admin/v1/tenants/registry",
            json={"tenant_name": "X", "status": "active"},
            headers={"Authorization": "Bearer frontend-token-xxx"},
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_mes_categories_endpoint_empty_and_sample_data() -> None:
    app, store = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        empty = await client.get(
            "/admin/v1/usage/mes-categories",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_viewer_headers(),
        )
        assert empty.status_code == 200
        body = empty.json()
        assert body["categories"] == {"output": 0, "payroll": 0, "order": 0, "other": 0}
        assert body["total"] == 0
        assert body["metric_version"].startswith("rollup=")
        assert body["timezone"] == "Asia/Shanghai"
        assert body["incomplete"] is False

    store.mes_call_facts = [
        MesCallFact(
            event_id="m-1",
            tenant_id="fac-01",
            session_id="s",
            interaction_id="i",
            occurred_at=NOW,
            operation_id="BarcodeClQuery",
            page_count=1,
            row_count_bucket="1-10",
            duration_ms=100,
            status="completed",
            error_category=None,
            received_at=NOW,
        ),
        MesCallFact(
            event_id="m-2",
            tenant_id="fac-01",
            session_id="s",
            interaction_id="i",
            occurred_at=NOW,
            operation_id="GongziMxQuery",
            page_count=1,
            row_count_bucket="1-10",
            duration_ms=100,
            status="failed",
            error_category="mes_timeout",
            received_at=NOW,
        ),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sample = await client.get(
            "/admin/v1/usage/mes-categories",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_viewer_headers(),
        )
        assert sample.status_code == 200
        assert sample.json()["categories"] == {"output": 1, "payroll": 0, "order": 0, "other": 0}
        assert sample.json()["total"] == 1
        failures = await client.get(
            "/admin/v1/usage/mes-failures",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_viewer_headers(),
        )
        assert failures.status_code == 200
        assert failures.json()["total"] == 1
        assert failures.json()["by_error"] == {"mes_timeout": 1}


@pytest.mark.asyncio
async def test_by_tenant_endpoint_masks_app_keys() -> None:
    app, store = make_app()
    store.tenant_registry = {
        "fac-0123456789": TenantRegistryRecord(
            app_key="fac-0123456789",
            tenant_name="温州一厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/v1/usage/by-tenant",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_analyst_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["app_key"] == "fac-01***"
        assert body["items"][0]["tenant_name"] == "温州一厂"
        assert body["items"][0]["status"] == "active"
        assert body["metric_version"].startswith("rollup=")


@pytest.mark.asyncio
async def test_app_keys_never_leak_unmasked_in_responses() -> None:
    app, store = make_app()
    store.tenant_registry = {
        "secret-key-987654": TenantRegistryRecord(
            app_key="secret-key-987654",
            tenant_name="保密工厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/admin/v1/tenants/registry", headers=_analyst_headers())
        assert listed.status_code == 200
        assert all("secret-key-987654" not in item["app_key"] for item in listed.json()["items"])
        detail = await client.get(
            "/admin/v1/tenants/registry/secret-key-987654", headers=_analyst_headers()
        )
        assert "secret-key-987654" not in detail.json()["app_key"]
        by_tenant = await client.get(
            "/admin/v1/usage/by-tenant",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_analyst_headers(),
        )
        serialized = by_tenant.text
        assert "secret-key-987654" not in serialized


@pytest.mark.asyncio
async def test_viewer_cannot_export_but_admin_can() -> None:
    app, _ = make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/admin/v1/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers=_viewer_headers(),
        )
        assert denied.status_code == 403
        allowed = await client.post(
            "/admin/v1/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers=_admin_headers(),
        )
        assert allowed.status_code == 201
