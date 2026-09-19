"""Dependency container for the statistics surface.

The container is built once per process by ``factory_agent.bootstrap`` and kept
on ``app.state.statistics`` so the statistics routes never read the business
container (or a business credential) to find their identity.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.statistics.alerts import AlertSink, LoggingAlertSink
from factory_agent.statistics.config import StatisticsSettings
from factory_agent.statistics.export_store import (
    ExportFileStore,
    InMemoryExportFileStore,
    LocalExportFileStore,
    S3ExportFileStore,
)
from factory_agent.statistics.exports import ExportService
from factory_agent.statistics.ops import OpsLimits, OpsService
from factory_agent.statistics.platform_auth import AuthService
from factory_agent.statistics.store import InMemoryUsageStore, PostgresUsageStore, UsageStore
from factory_agent.statistics.tenant_registration import StatisticsTenantRegistryWriter
from factory_agent.statistics.tenants import TenantRegistryService

_LOGGER = get_logger("factory_agent.statistics.container")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def build_export_file_store(settings: StatisticsSettings) -> ExportFileStore:
    """Select the statistics export backend from configuration.

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
    _LOGGER.warning("statistics.export.store_in_memory")
    return InMemoryExportFileStore()


@dataclass(frozen=True, slots=True)
class StatisticsContainer:
    settings: StatisticsSettings
    store: UsageStore
    ops: OpsService
    exports: ExportService
    files: ExportFileStore
    clock: Callable[[], datetime]
    new_id: Callable[[], str]
    auth: AuthService
    tenants: TenantRegistryService
    alerts: AlertSink

    def tenant_registration(self) -> StatisticsTenantRegistryWriter | None:
        """First-seen AppKey registration; absent without a durable store.

        Registration writes the same table the tenant service manages, so the
        writer derives from the live store instead of being a second
        implementation of that table. The concrete type is returned rather than
        the ``TenantRegistryWriter`` protocol: this package deliberately does not
        depend on ``factory_agent.ports``, and the caller's protocol assignment
        is checked structurally.
        """
        if isinstance(self.store, PostgresUsageStore):
            return StatisticsTenantRegistryWriter(self.store, clock=self.clock)
        return None


def build_container(
    settings: StatisticsSettings,
    *,
    database_url: str | None = None,
    store: UsageStore | None = None,
    files: ExportFileStore | None = None,
    clock: Callable[[], datetime] | None = None,
    new_id: Callable[[], str] | None = None,
    alerts: AlertSink | None = None,
    limits: OpsLimits | None = None,
) -> StatisticsContainer:
    active_clock = clock or SystemClock().now
    active_new_id = new_id or (lambda: uuid4().hex)

    if store is not None:
        active_store = store
    elif database_url:
        active_store = PostgresUsageStore(
            database_url, statement_timeout_ms=settings.statement_timeout_ms
        )
    else:
        active_store = InMemoryUsageStore()

    active_files = files if files is not None else build_export_file_store(settings)
    ops = OpsService(
        active_store,
        clock=active_clock,
        timezone_name=settings.timezone_name,
        limits=limits or OpsLimits(),
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
    return StatisticsContainer(
        settings=settings,
        store=active_store,
        ops=ops,
        exports=exports,
        files=active_files,
        clock=active_clock,
        new_id=active_new_id,
        auth=auth,
        tenants=tenants,
        alerts=alerts if alerts is not None else LoggingAlertSink(),
    )


__all__ = [
    "InMemoryExportFileStore",
    "StatisticsContainer",
    "SystemClock",
    "build_container",
    "build_export_file_store",
]
