"""Dependency container for the usage-admin API."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from usage_admin.alerts import LoggingAlertSink
from usage_admin.auth import AuthService
from usage_admin.config import UsageAdminSettings
from usage_admin.exports import ExportFileStore, ExportService
from usage_admin.ingest import IngestLimits, IngestService
from usage_admin.ops import OpsLimits, OpsService
from usage_admin.store import InMemoryUsageStore, PostgresUsageStore, UsageStore
from usage_admin.tenants import TenantRegistryService


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class InMemoryExportFileStore:
    """Dev/test export file store; production should mount a real object store."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self._blobs[key] = data

    async def get(self, key: str) -> bytes | None:
        return self._blobs.get(key)

    async def delete(self, key: str) -> None:
        self._blobs.pop(key, None)

    def blob_keys(self) -> tuple[str, ...]:
        return tuple(self._blobs)


@dataclass(frozen=True, slots=True)
class AdminContainer:
    settings: UsageAdminSettings
    store: UsageStore
    ingest: IngestService
    ops: OpsService
    exports: ExportService
    files: ExportFileStore
    clock: Callable[[], datetime]
    new_id: Callable[[], str]
    auth: AuthService
    tenants: TenantRegistryService


def build_container(
    settings: UsageAdminSettings,
    *,
    store: UsageStore | None = None,
    files: ExportFileStore | None = None,
    clock: Callable[[], datetime] | None = None,
    new_id: Callable[[], str] | None = None,
) -> AdminContainer:
    active_clock = clock or SystemClock().now
    active_new_id = new_id or (lambda: uuid4().hex)

    if store is not None:
        active_store = store
    elif settings.database_url is not None:
        active_store = PostgresUsageStore(settings.database_url.get_secret_value())
    else:
        active_store = InMemoryUsageStore()

    active_files = files or InMemoryExportFileStore()
    ingest_limits = IngestLimits(
        max_events=settings.ingest_batch_max_events,
        max_bytes=settings.ingest_batch_max_bytes,
    )
    ingest = IngestService(
        active_store,
        clock=active_clock,
        limits=ingest_limits,
        alerts=LoggingAlertSink(),
    )
    ops = OpsService(
        active_store,
        clock=active_clock,
        timezone_name=settings.timezone_name,
        limits=OpsLimits(),
    )
    signing_secret = (
        settings.export_signing_secret.get_secret_value()
        if settings.export_signing_secret is not None
        else secrets.token_hex(16)
    )
    exports = ExportService(
        active_store,
        ops,
        active_files,
        clock=active_clock,
        new_id=active_new_id,
        signing_secret=signing_secret,
        download_base_url=settings.download_base_url,
        presign_expires_seconds=settings.export_presign_expires_seconds,
    )
    token_signing_secret = (
        settings.token_signing_secret.get_secret_value()
        if settings.token_signing_secret is not None
        else secrets.token_hex(16)
    )
    auth = AuthService(
        active_store,
        clock=active_clock,
        new_id=active_new_id,
        signing_secret=token_signing_secret,
        api_token=settings.api_token.get_secret_value() if settings.api_token is not None else None,
        token_ttl_seconds=settings.token_ttl_seconds,
    )
    tenants = TenantRegistryService(
        active_store,
        clock=active_clock,
        new_id=active_new_id,
    )
    return AdminContainer(
        settings=settings,
        store=active_store,
        ingest=ingest,
        ops=ops,
        exports=exports,
        files=active_files,
        clock=active_clock,
        new_id=active_new_id,
        auth=auth,
        tenants=tenants,
    )


__all__ = [
    "AdminContainer",
    "InMemoryExportFileStore",
    "SystemClock",
    "build_container",
]
