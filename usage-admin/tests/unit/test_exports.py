"""Export generation, short-lived signed links, and audit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from support.events import interaction_started
from usage_admin.container import InMemoryExportFileStore
from usage_admin.exports import (
    ExportService,
    render_csv,
    render_xlsx,
    sign_download,
    verify_download,
)
from usage_admin.ingest import IngestService
from usage_admin.ops import OpsService
from usage_admin.platform import PlatformRole, PlatformScope, PlatformScopeError
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SECRET = "test-secret"


def make_service() -> tuple[ExportService, InMemoryUsageStore, InMemoryExportFileStore]:
    store = InMemoryUsageStore()
    files = InMemoryExportFileStore()
    ops = OpsService(store, clock=lambda: NOW)
    counter = iter(range(1000))
    service = ExportService(
        store,
        ops,
        files,
        clock=lambda: NOW,
        new_id=lambda: f"export-{next(counter)}",
        signing_secret=SECRET,
        download_base_url="http://usage-admin.test",
    )
    return service, store, files


def analyst() -> PlatformScope:
    return PlatformScope("ops-1", PlatformRole.ANALYST, frozenset())


def viewer() -> PlatformScope:
    return PlatformScope("ops-1", PlatformRole.VIEWER, frozenset())


def test_render_csv_and_xlsx_produce_expected_bytes() -> None:
    from usage_admin.exports import ExportTable

    table = ExportTable(columns=("a", "b"), rows=((1, "x"), (2, "y")))
    csv_bytes = render_csv(table)
    assert csv_bytes == b"a,b\n1,x\n2,y\n"
    xlsx_bytes = render_xlsx(table)
    assert xlsx_bytes[:2] == b"PK"  # xlsx is a zip container


def test_signed_download_tokens_verify_and_expire() -> None:
    token = sign_download(SECRET, "export-1", NOW + timedelta(minutes=5))
    assert verify_download(SECRET, token, now=NOW) == "export-1"
    assert verify_download(SECRET, token, now=NOW + timedelta(minutes=10)) is None
    assert verify_download("wrong-secret", token, now=NOW) is None
    assert verify_download(SECRET, "garbage", now=NOW) is None


@pytest.mark.asyncio
async def test_only_analyst_can_create_export() -> None:
    service, _, _ = make_service()

    with pytest.raises(PlatformScopeError, match="analyst"):
        await service.create_export(viewer(), start=START, end=END, format="csv")


@pytest.mark.asyncio
async def test_create_export_builds_file_and_audits() -> None:
    service, store, files = make_service()
    await IngestService(store, clock=lambda: NOW).ingest(
        [interaction_started("s-1", user_subject_id="u" * 64)]
    )

    view = await service.create_export(analyst(), start=START, end=END, format="csv")

    assert view.status == "ready"
    assert view.download_url is not None
    assert view.download_url.startswith("http://usage-admin.test")
    assert len(files.blob_keys()) == 1
    assert any(entry.action == "export.create" for entry in store.audits)


@pytest.mark.asyncio
async def test_download_resolves_token_and_returns_bytes() -> None:
    service, store, _ = make_service()
    await IngestService(store, clock=lambda: NOW).ingest([interaction_started("s-1")])

    view = await service.create_export(analyst(), start=START, end=END, format="csv")
    assert view.download_url is not None
    token = view.download_url.split("token=")[1]

    result = await service.download(token)
    assert result is not None
    data, format = result
    assert format == "csv"
    assert data.startswith(b"users,")


@pytest.mark.asyncio
async def test_expired_or_tampered_token_returns_none() -> None:
    service, store, _ = make_service()
    await IngestService(store, clock=lambda: NOW).ingest([interaction_started("s-1")])

    view = await service.create_export(analyst(), start=START, end=END, format="csv")
    assert view.download_url is not None
    token = view.download_url.split("token=")[1]

    assert await service.download(token + "x") is None


@pytest.mark.asyncio
async def test_export_supports_mes_category_metrics() -> None:
    from usage_admin.events import MesCallFact

    service, store, _ = make_service()
    store.mes_call_facts = [
        MesCallFact(
            event_id="m-1",
            tenant_id="tenant-a",
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
            tenant_id="tenant-a",
            session_id="s",
            interaction_id="i",
            occurred_at=NOW,
            operation_id="GongziMxQuery",
            page_count=1,
            row_count_bucket="1-10",
            duration_ms=100,
            status="completed",
            error_category=None,
            received_at=NOW,
        ),
    ]

    view = await service.create_export(
        analyst(),
        start=START,
        end=END,
        format="csv",
        metrics=("mes_output", "mes_payroll", "mes_order", "mes_other"),
    )
    assert view.download_url is not None
    token = view.download_url.split("token=")[1]

    result = await service.download(token)
    assert result is not None
    data, _ = result
    header = data.split(b"\n")[0].decode()
    assert "mes_output" in header
    assert "mes_payroll" in header
    assert b"mes_output,mes_payroll" in data.split(b"\n")[0]
    row = data.split(b"\n")[1].decode()
    assert row.endswith(",1,1,0,0")


@pytest.mark.asyncio
async def test_export_record_tenant_filter_is_masked() -> None:
    service, store, _ = make_service()

    await service.create_export(
        PlatformScope("ops-1", PlatformRole.ANALYST, frozenset({"secret-key-987654"})),
        start=START,
        end=END,
        format="csv",
    )

    record = next(iter(store.exports.values()))
    serialized = str(record.tenant_filter)
    assert "secret-key-987654" not in serialized
    filtered = record.tenant_filter.get("tenant_ids", [])
    assert isinstance(filtered, list)
    assert "secret-key-987654" not in filtered
