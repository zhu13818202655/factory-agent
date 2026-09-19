"""API-level tests for the statistics endpoints."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

from factory_agent.statistics.config import StatisticsSettings
from factory_agent.statistics.container import build_container
from factory_agent.statistics.events import MesCallFact
from factory_agent.statistics.store import InMemoryUsageStore, RollupRow, TenantRegistryRecord
from tests.statistics.support.app import build_statistics_app
from tests.statistics.support.events import interaction_started
from tests.statistics.support.tokens import bearer, issue_platform_token

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SECRET = "test-signing-secret"


def make_app(
    *,
    api_token: str | None = None,
) -> tuple[httpx.ASGITransport, InMemoryUsageStore, str]:
    store = InMemoryUsageStore()
    settings = StatisticsSettings(
        export_signing_secret=SecretStr("test-secret"),
        download_base_url="http://test",
        api_token=SecretStr(api_token) if api_token else None,
        token_signing_secret=SecretStr(SECRET),
    )
    container = build_container(
        settings,
        store=store,
        clock=lambda: NOW,
        new_id=lambda: "export-1",
    )
    return httpx.ASGITransport(app=build_statistics_app(container)), store, SECRET


def _admin_headers(secret: str) -> dict[str, str]:
    return bearer(issue_platform_token(secret, role="admin"))


def _analyst_headers(secret: str) -> dict[str, str]:
    return bearer(issue_platform_token(secret, role="analyst"))


def _viewer_headers(secret: str) -> dict[str, str]:
    return bearer(issue_platform_token(secret, role="viewer"))


def _seed_summary_data(store: InMemoryUsageStore) -> None:
    """Seed one interaction fact plus the matching rollup row."""
    store.interaction_facts = [interaction_started("s-1", user_subject_id="u" * 64)]
    store.rollup_rows = [
        RollupRow(
            tenant_id="tenant-a",
            bucket_start=START,
            metric="questions",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        )
    ]


@pytest.mark.asyncio
async def test_statistics_endpoints_reject_anonymous_requests() -> None:
    transport, _, _ = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/statistics/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_statistics_endpoints_reject_unverified_identity_headers() -> None:
    """D-2: asserting a principal and a role in headers is not authentication."""
    transport, _, _ = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/statistics/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers={
                "X-Platform-Principal": "ops-1",
                "X-Platform-Role": "admin",
                "X-Platform-Tenants": "",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_summary_endpoint_returns_metrics() -> None:
    transport, store, secret = make_app()
    _seed_summary_data(store)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/statistics/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_analyst_headers(secret),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["questions"] == 1
    assert body["metric_version"].startswith("rollup=")
    assert body["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_exports_require_analyst_role() -> None:
    transport, _, secret = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/statistics/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers=_viewer_headers(secret),
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_export_download_roundtrip() -> None:
    transport, store, secret = make_app()
    _seed_summary_data(store)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/statistics/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers=_analyst_headers(secret),
        )
        assert created.status_code == 201
        url = created.json()["download_url"]
        download = await client.get(url)

    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert download.content.startswith(b"users,")


@pytest.mark.asyncio
async def test_over_span_query_is_rejected_with_422() -> None:
    transport, _, secret = make_app()
    wide_start = END - timedelta(days=400)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/statistics/usage/summary",
            params={"start": wide_start.isoformat(), "end": END.isoformat()},
            headers=_analyst_headers(secret),
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_registry_crud_requires_admin_for_writes() -> None:
    transport, _, secret = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # analyst may read the list but not create.
        listed = await client.get(
            "/v1/statistics/tenants/registry", headers=_analyst_headers(secret)
        )
        assert listed.status_code == 200
        created = await client.post(
            "/v1/statistics/tenants/registry",
            json={"tenant_name": "温州一厂", "status": "active"},
            headers=_analyst_headers(secret),
        )
        assert created.status_code == 403
        # admin creates.
        created = await client.post(
            "/v1/statistics/tenants/registry",
            json={"tenant_name": "温州一厂", "status": "active"},
            headers=_admin_headers(secret),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["tenant_name"] == "温州一厂"
        assert body["status"] == "active"
        # create response carries the plaintext AppKey exactly once (D9).
        plaintext_key = body["app_key"]
        assert plaintext_key.startswith("fac-")
        assert "***" not in plaintext_key
        # ... and the non-secret ref, which is what everything else addresses.
        tenant_ref = body["tenant_ref"]
        assert tenant_ref.startswith("t_")
        # every read response masks the AppKey but returns the ref verbatim.
        detail = await client.get(
            f"/v1/statistics/tenants/registry/{tenant_ref}", headers=_admin_headers(secret)
        )
        assert detail.status_code == 200
        assert detail.json()["app_key"] == f"{plaintext_key[:6]}***"
        assert detail.json()["tenant_ref"] == tenant_ref


@pytest.mark.asyncio
async def test_registry_delete_is_disable_and_enable_reactivates() -> None:
    transport, store, secret = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/statistics/tenants/registry",
            json={"tenant_name": "A", "status": "active"},
            headers=_admin_headers(secret),
        )
        tenant_ref = created.json()["tenant_ref"]
        app_key = created.json()["app_key"]
        deleted = await client.delete(
            f"/v1/statistics/tenants/registry/{tenant_ref}", headers=_admin_headers(secret)
        )
        assert deleted.status_code == 204
        detail = await client.get(
            f"/v1/statistics/tenants/registry/{tenant_ref}", headers=_admin_headers(secret)
        )
        assert detail.json()["status"] == "disabled"
        # history is preserved: the record still exists.
        assert store.tenant_registry[app_key].status == "disabled"
        enabled = await client.post(
            f"/v1/statistics/tenants/registry/{tenant_ref}/enable", headers=_admin_headers(secret)
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"] == "active"


@pytest.mark.asyncio
async def test_auth_register_login_and_bearer_flow() -> None:
    transport, _, secret = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # non-admin cannot register.
        denied = await client.post(
            "/v1/statistics/auth/register",
            json={"username": "ops", "password": "password-123", "role": "viewer"},
            headers=_analyst_headers(secret),
        )
        assert denied.status_code == 403
        registered = await client.post(
            "/v1/statistics/auth/register",
            json={"username": "ops", "password": "password-123", "role": "admin"},
            headers=_admin_headers(secret),
        )
        assert registered.status_code == 201
        assert registered.json()["role"] == "admin"
        logged = await client.post(
            "/v1/statistics/auth/login",
            json={"username": "ops", "password": "password-123"},
        )
        assert logged.status_code == 200
        token = logged.json()["token"]
        # the issued bearer token authenticates the same admin scope.
        summary = await client.get(
            "/v1/statistics/usage/summary",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=bearer(token),
        )
        assert summary.status_code == 200
        # a bad password never yields a token.
        failed = await client.post(
            "/v1/statistics/auth/login",
            json={"username": "ops", "password": "wrong-password"},
        )
        assert failed.status_code == 401


@pytest.mark.asyncio
async def test_frontend_api_token_bearer_channel() -> None:
    transport, _, _ = make_app(api_token="frontend-token-abc")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/statistics/tenants/registry",
            json={"tenant_name": "前端工厂", "status": "active"},
            headers=bearer("frontend-token-abc"),
        )
        assert created.status_code == 201
        # a tampered token is rejected with 403.
        denied = await client.post(
            "/v1/statistics/tenants/registry",
            json={"tenant_name": "X", "status": "active"},
            headers=bearer("frontend-token-xxx"),
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_mes_categories_endpoint_empty_and_sample_data() -> None:
    transport, store, secret = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        empty = await client.get(
            "/v1/statistics/usage/mes-categories",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_viewer_headers(secret),
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
            "/v1/statistics/usage/mes-categories",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_viewer_headers(secret),
        )
        assert sample.status_code == 200
        assert sample.json()["categories"] == {"output": 1, "payroll": 0, "order": 0, "other": 0}
        assert sample.json()["total"] == 1
        failures = await client.get(
            "/v1/statistics/usage/mes-failures",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_viewer_headers(secret),
        )
        assert failures.status_code == 200
        assert failures.json()["total"] == 1
        assert failures.json()["by_error"] == {"mes_timeout": 1}


@pytest.mark.asyncio
async def test_by_tenant_endpoint_masks_app_keys() -> None:
    transport, store, secret = make_app()
    store.tenant_registry = {
        "fac-0123456789": TenantRegistryRecord(
            app_key="fac-0123456789",
            tenant_ref="t_11112222",
            tenant_name="温州一厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/statistics/usage/by-tenant",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_analyst_headers(secret),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["app_key"] == "fac-01***"
        assert body["items"][0]["tenant_name"] == "温州一厂"
        assert body["items"][0]["status"] == "active"
        assert body["metric_version"].startswith("rollup=")


@pytest.mark.asyncio
async def test_app_keys_never_leak_unmasked_in_responses() -> None:
    transport, store, secret = make_app()
    store.tenant_registry = {
        "secret-key-987654": TenantRegistryRecord(
            app_key="secret-key-987654",
            tenant_ref="t_33334444",
            tenant_name="保密工厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            "/v1/statistics/tenants/registry", headers=_analyst_headers(secret)
        )
        assert listed.status_code == 200
        assert all("secret-key-987654" not in item["app_key"] for item in listed.json()["items"])
        detail = await client.get(
            "/v1/statistics/tenants/registry/t_33334444", headers=_analyst_headers(secret)
        )
        assert "secret-key-987654" not in detail.json()["app_key"]
        # addressing by masked AppKey is not an access path.
        masked_lookup = await client.get(
            "/v1/statistics/tenants/registry/secret-key-987654", headers=_analyst_headers(secret)
        )
        assert masked_lookup.status_code == 404
        by_tenant = await client.get(
            "/v1/statistics/usage/by-tenant",
            params={"start": START.isoformat(), "end": END.isoformat()},
            headers=_analyst_headers(secret),
        )
        serialized = by_tenant.text
        assert "secret-key-987654" not in serialized


@pytest.mark.asyncio
async def test_factory_selector_hands_out_handles_not_app_keys() -> None:
    """The usage board's factory picker must not be a key-distribution channel.

    It used to return the plaintext AppKeys of every tenant with data in the
    window, which is how the dashboard ended up displaying them. It now returns
    the non-secret ``tenant_ref`` + name, and the exact-factory filter moved to
    ``tenant_ref`` accordingly.
    """
    transport, store, secret = make_app()
    store.tenant_registry = {
        "secret-key-987654": TenantRegistryRecord(
            app_key="secret-key-987654",
            tenant_ref="t_33334444",
            tenant_name="保密工厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
    }
    store.mes_call_facts = [
        MesCallFact(
            event_id="m-1",
            tenant_id="secret-key-987654",
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
        )
    ]
    params = {"start": START.isoformat(), "end": END.isoformat()}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        selector = await client.get(
            "/v1/statistics/tenants", params=params, headers=_viewer_headers(secret)
        )
        assert selector.status_code == 200
        assert selector.json() == [{"tenant_ref": "t_33334444", "tenant_name": "保密工厂"}]
        assert "secret-key-987654" not in selector.text

        filtered = await client.get(
            "/v1/statistics/usage/by-tenant",
            params={**params, "tenant_ref": "t_33334444"},
            headers=_viewer_headers(secret),
        )
        assert filtered.status_code == 200
        assert [item["tenant_name"] for item in filtered.json()["items"]] == ["保密工厂"]

        # The plaintext-key filter is gone; a stale caller is told, not silently ignored.
        legacy = await client.get(
            "/v1/statistics/usage/by-tenant",
            params={**params, "app_key": "secret-key-987654"},
            headers=_viewer_headers(secret),
        )
        assert legacy.status_code == 422
        assert "tenant_ref" in legacy.json()["detail"]


AGGREGATE_PATHS: tuple[tuple[str, dict[str, str]], ...] = (
    ("/v1/statistics/usage/summary", {}),
    ("/v1/statistics/usage/timeseries", {}),
    ("/v1/statistics/usage/dimensions", {"dimension": "capability"}),
    ("/v1/statistics/usage/users", {}),
    ("/v1/statistics/usage/mes-categories", {}),
    ("/v1/statistics/usage/mes-failures", {}),
    ("/v1/statistics/usage/mes-operations", {}),
    ("/v1/statistics/usage/models", {}),
    ("/v1/statistics/usage/capabilities", {}),
    ("/v1/statistics/usage/errors", {}),
)


def _seed_two_factories(store: InMemoryUsageStore) -> None:
    store.tenant_registry = {
        "secret-key-987654": TenantRegistryRecord(
            app_key="secret-key-987654",
            tenant_ref="t_33334444",
            tenant_name="保密工厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        ),
        "other-key-123456": TenantRegistryRecord(
            app_key="other-key-123456",
            tenant_ref="t_55556666",
            tenant_name="另一工厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        ),
    }
    store.mes_call_facts = [
        MesCallFact(
            event_id=f"m-{index}",
            tenant_id=app_key,
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
        )
        for index, app_key in enumerate(("secret-key-987654", "other-key-123456"))
    ]


@pytest.mark.asyncio
async def test_factory_filter_applies_to_every_aggregate_endpoint() -> None:
    """``tenant_ref`` is one chokepoint, so it must narrow every report equally.

    Only ``by-tenant`` used to take a factory filter; the rest silently ignored
    one and answered platform-wide. Every aggregate now narrows to the handle.
    """
    transport, store, secret = make_app()
    _seed_two_factories(store)
    params = {"start": START.isoformat(), "end": END.isoformat()}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path, extra in AGGREGATE_PATHS:
            response = await client.get(
                path,
                params={**params, **extra, "tenant_ref": "t_33334444"},
                headers=_viewer_headers(secret),
            )
            assert response.status_code == 200, (path, response.text)
            # One factory in, and never the plaintext key in the echo.
            assert response.json()["tenant_ids"] == ["secret***"], path
            assert "secret-key-987654" not in response.text, path

        unknown = await client.get(
            "/v1/statistics/usage/summary",
            params={**params, "tenant_ref": "t_deadbeef"},
            headers=_viewer_headers(secret),
        )
        assert unknown.status_code == 422
        assert "unknown tenant_ref" in unknown.json()["detail"]


@pytest.mark.asyncio
async def test_a_scoped_token_cannot_filter_on_a_factory_outside_its_scope() -> None:
    transport, store, secret = make_app()
    _seed_two_factories(store)
    scoped = bearer(issue_platform_token(secret, role="viewer", tenant_ids=("secret-key-987654",)))
    params = {"start": START.isoformat(), "end": END.isoformat()}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        own = await client.get(
            "/v1/statistics/usage/summary",
            params={**params, "tenant_ref": "t_33334444"},
            headers=scoped,
        )
        assert own.status_code == 200
        assert own.json()["tenant_ids"] == ["secret***"]

        foreign = await client.get(
            "/v1/statistics/usage/summary",
            params={**params, "tenant_ref": "t_55556666"},
            headers=scoped,
        )
        assert foreign.status_code == 403


@pytest.mark.asyncio
async def test_viewer_cannot_export_but_admin_can() -> None:
    transport, _, secret = make_app()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/v1/statistics/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers=_viewer_headers(secret),
        )
        assert denied.status_code == 403
        allowed = await client.post(
            "/v1/statistics/exports",
            json={"start": START.isoformat(), "end": END.isoformat(), "format": "csv"},
            headers=_admin_headers(secret),
        )
        assert allowed.status_code == 201
