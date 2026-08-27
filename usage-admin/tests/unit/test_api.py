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
from usage_admin.ingest import IngestService
from usage_admin.platform import PRINCIPAL_HEADER, ROLE_HEADER
from usage_admin.rollup import RollupEngine
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def make_app(*, api_key: str | None = None):
    store = InMemoryUsageStore()
    settings = UsageAdminSettings(
        database_url=None,
        ingest_api_key=SecretStr(api_key) if api_key else None,
        export_signing_secret=SecretStr("test-secret"),
        download_base_url="http://usage-admin.test",
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
