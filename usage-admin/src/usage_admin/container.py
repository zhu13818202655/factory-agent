"""Dependency container for the usage-admin API."""



import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from usage_admin.auth import AuthService
from usage_admin.config import UsageAdminSettings
from usage_admin.export_store import (
    ExportFileStore,
    InMemoryExportFileStore,
    LocalExportFileStore,
    S3ExportFileStore,
)
from usage_admin.exports import ExportService
from usage_admin.logging import get_logger
from usage_admin.ops import OpsLimits, OpsService
from usage_admin.store import InMemoryUsageStore, PostgresUsageStore, UsageStore
from usage_admin.tenants import TenantRegistryService

_LOGGER = get_logger("usage_admin.container")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def build_export_file_store(settings: UsageAdminSettings) -> ExportFileStore:
    """Select the export backend from configuration.

    S3 wins when an endpoint is configured, then the local directory, and the
    in-memory store is the dev/test fallback — it is announced because exports
    written to it are gone after a restart.
    """
    if settings.s3_endpoint_url.strip():
        return S3ExportFileStore(
            endpoint_url=settings.s3_endpoint_url,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
            region=settings.s3_region,
            path_style=settings.s3_path_style,
        )
    if settings.export_store_dir.strip():
        return LocalExportFileStore(Path(settings.export_store_dir))
    _LOGGER.warning("export.store.in_memory")
    return InMemoryExportFileStore()


@dataclass(frozen=True, slots=True)
class AdminContainer:
    settings: UsageAdminSettings
    store: UsageStore
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

    active_files = files if files is not None else build_export_file_store(settings)
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
    "build_export_file_store",
]
