"""Fetch whole-tenant snapshots under a separately verified owner identity.

An interaction may already bind another caller's bundle. Both authorization and
snapshot HTTP calls therefore bind the owner explicitly, and token refresh must
revalidate that identity before the adapter may retry a business request. Secret
configuration is consumed and retained inside this package only.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import CURRENT_BUNDLE, MesCredentialBundle
from factory_agent.data_api.hongzhao import AdapterSettings, HongzhaoMesAdapter
from factory_agent.data_api.pagination import PagerBudget
from factory_agent.data_api.token_gateway import TokenCredentialExchange
from factory_agent.domain import DataScope, NarrowedFilters, Role, TenantContext, TenantId
from factory_agent.domain.errors import (
    ForbiddenError,
    InvalidRequestError,
    TenantDisabledError,
    UnsupportedOperationError,
    UpstreamUnavailableError,
)
from factory_agent.observability.context import bind_tenant_id, current_tenant_id
from factory_agent.ports import Clock, MesCallRecorder
from factory_agent.ports.contracts import ResourceFetchResult, TrustedCredential
from factory_agent.ports.tenant_registry import TenantRegistryReader


class SecretCredentialReference(Protocol):
    def get_secret_value(self) -> str: ...


class SnapshotAuthorizer(Protocol):
    async def __call__(
        self, credential: TrustedCredential, as_of: datetime
    ) -> tuple[TenantContext, DataScope]: ...


class OwnerSnapshotConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("owner snapshot credential configuration is invalid")


class OwnerSnapshotUnavailableError(UpstreamUnavailableError):
    def __init__(self) -> None:
        super().__init__("an authorized owner snapshot is unavailable")


class OwnerSnapshotFetcher:
    """One encrypted credential's isolated, revalidated snapshot adapter."""

    def __init__(
        self,
        exchange: TokenCredentialExchange,
        encrypted_credential: str,
        base_url: str,
        *,
        authorize: SnapshotAuthorizer,
        clock: Clock,
        adapter_settings: AdapterSettings,
        pager_budget: PagerBudget,
        tenant_registry: TenantRegistryReader | None = None,
        recorder: MesCallRecorder | None = None,
    ) -> None:
        if not encrypted_credential.strip():
            raise OwnerSnapshotConfigurationError()
        self._exchange = exchange
        self._encrypted_credential = encrypted_credential.strip()
        self._base_url = base_url
        self._authorize = authorize
        self._clock = clock
        self._settings = adapter_settings
        self._pager_budget = pager_budget
        self._registry = tenant_registry
        self._recorder = recorder
        self._timezone = ZoneInfo(adapter_settings.factory_timezone)
        self._identity: TrustedCredential | None = None
        self._authorization: tuple[TenantContext, DataScope] | None = None
        self._adapter: HongzhaoMesAdapter | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def tenant_id(self) -> TenantId | None:
        return self._identity.tenant_id if self._identity is not None else None

    async def initialize(self) -> None:
        """Verify the configured owner and tenant without fetching business rows."""
        async with self._lock:
            self._assert_open()
            await self._resolve_owner()

    async def authorization_context(self) -> tuple[TenantContext, DataScope]:
        """Expose freshly authorized identity facts for operational recipe checks."""
        async with self._lock:
            self._assert_open()
            await self._resolve_owner()
            if self._authorization is None:  # pragma: no cover - set by _validate_owner
                raise OwnerSnapshotUnavailableError()
            return self._authorization

    async def __call__(
        self,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult:
        return await self._fetch(None, operation_id, window, extra_params)

    async def fetch_for(
        self,
        tenant_id: TenantId,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult:
        """Fetch only when the exchanged owner belongs to the requested tenant."""
        return await self._fetch(tenant_id, operation_id, window, extra_params)

    async def _fetch(
        self,
        expected_tenant: TenantId | None,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult:
        params = _snapshot_params(operation_id, extra_params)
        interval = self._calendar_interval(window)
        async with self._lock:
            self._assert_open()
            credential = await self._resolve_owner(expected_tenant)
            adapter = self._adapter
            if adapter is None:  # pragma: no cover - established by _resolve_owner
                raise OwnerSnapshotUnavailableError()
            filters = NarrowedFilters(
                tenant_id=credential.tenant_id, employee_ids=None, dept_ids=None
            )
            caller_tenant = current_tenant_id()
            try:
                with self._exchange.bind_for(credential):
                    bind_tenant_id(str(credential.tenant_id))
                    return await adapter.fetch_resource(
                        operation_id,
                        filters,
                        interval,
                        page_size=self._pager_budget.page_size,
                        extra_params=params,
                    )
            finally:
                bind_tenant_id(caller_tenant)

    async def _resolve_owner(self, expected_tenant: TenantId | None = None) -> TrustedCredential:
        try:
            principal = await self._exchange.authenticate(self._encrypted_credential)
        except Exception:  # noqa: BLE001 - exchange failures must not expose secret configuration
            raise OwnerSnapshotUnavailableError() from None
        credential = principal.credential
        if principal.role is not Role.OWNER:
            raise ForbiddenError("snapshot identity is not an owner")
        if expected_tenant is not None and credential.tenant_id != expected_tenant:
            raise ForbiddenError("snapshot identity belongs to another tenant")
        if self._identity is not None and credential != self._identity:
            raise ForbiddenError("snapshot identity changed")
        await self._validate_owner(credential)
        self._identity = credential
        if self._adapter is None:
            bundle = self._exchange.bundle_for(credential)
            if bundle is None:  # pragma: no cover - checked by _validate_owner
                raise OwnerSnapshotUnavailableError()
            self._adapter = HongzhaoMesAdapter(
                self._base_url,
                bundle,
                load_catalog(),
                refresher=_SnapshotTokenRefresher(lambda: self._refresh(credential)),
                settings=self._settings,
                clock=self._clock,
                pager_budget=self._pager_budget,
                recorder=self._recorder,
                tenant_registry=self._registry,
            )
        return credential

    async def _validate_owner(self, credential: TrustedCredential) -> None:
        principal = self._exchange.principal_for(credential)
        bundle = self._exchange.bundle_for(credential)
        if (
            principal is None
            or bundle is None
            or principal.credential != credential
            or principal.role is not Role.OWNER
            or bundle.tenant_id != credential.tenant_id
            or bundle.user != credential.user_id
            or not bundle.roles
            or bundle.roles[0] != Role.OWNER.mes_code
        ):
            raise ForbiddenError("snapshot owner identity is inconsistent")
        await self._assert_active(credential.tenant_id)
        caller_tenant = current_tenant_id()
        try:
            with self._exchange.bind_for(credential):
                context, scope = await self._authorize(credential, self._clock.now())
        except Exception:  # noqa: BLE001 - only sanitized authorization failures leave this boundary
            raise ForbiddenError("snapshot owner authorization failed") from None
        finally:
            bind_tenant_id(caller_tenant)
        if (
            context.role is not Role.OWNER
            or context.tenant_id != credential.tenant_id
            or context.user_id != credential.user_id
            or scope.tenant_id != credential.tenant_id
            or not scope.mes_filtered
        ):
            raise ForbiddenError("snapshot owner scope is inconsistent")
        self._authorization = context, scope

    async def _assert_active(self, tenant_id: TenantId) -> None:
        if self._registry is None:
            return
        try:
            record = await self._registry.get(str(tenant_id))
        except Exception:  # noqa: BLE001 - persistence errors must not expose connection details
            raise OwnerSnapshotUnavailableError() from None
        if record is not None:
            if record.app_key != str(tenant_id):
                raise ForbiddenError("snapshot tenant registry is inconsistent")
            if record.status != "active":
                raise TenantDisabledError()

    async def _refresh(self, credential: TrustedCredential) -> MesCredentialBundle:
        try:
            refreshed = await self._exchange.refresh_bundle(
                credential.tenant_id, credential.user_id
            )
        except Exception:  # noqa: BLE001 - stored encrypted credentials remain private
            raise OwnerSnapshotUnavailableError() from None
        if refreshed.tenant_id != credential.tenant_id or refreshed.user != credential.user_id:
            raise ForbiddenError("snapshot identity changed during refresh")
        await self._validate_owner(credential)
        CURRENT_BUNDLE.set(refreshed)
        return refreshed

    def _calendar_interval(self, window: tuple[str, str]) -> tuple[datetime, datetime]:
        try:
            start, end = (date.fromisoformat(value) for value in window)
            if start.isoformat() != window[0] or end.isoformat() != window[1] or start > end:
                raise ValueError
            return (
                datetime.combine(start, time.min, self._timezone),
                datetime.combine(end + timedelta(days=1), time.min, self._timezone),
            )
        except (ValueError, OverflowError):
            raise InvalidRequestError("snapshot calendar window is invalid") from None

    def _assert_open(self) -> None:
        if self._closed:
            raise OwnerSnapshotUnavailableError()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            if self._adapter is not None:
                adapter, self._adapter = self._adapter, None
                await adapter.aclose()


class _SnapshotTokenRefresher:
    def __init__(self, refresh: Callable[[], Awaitable[MesCredentialBundle]]) -> None:
        self._refresh = refresh

    async def refresh(self) -> MesCredentialBundle:
        return await self._refresh()


class OwnerSnapshotPool:
    """Select a verified owner from the existing secret setting per tenant."""

    def __init__(
        self,
        exchange: TokenCredentialExchange,
        credential_reference: SecretCredentialReference,
        base_url: str,
        *,
        authorize: SnapshotAuthorizer,
        clock: Clock,
        adapter_settings: AdapterSettings,
        pager_budget: PagerBudget,
        tenant_registry: TenantRegistryReader | None = None,
        recorder: MesCallRecorder | None = None,
    ) -> None:
        credentials = _configured_credentials(credential_reference)
        self.fetchers = tuple(
            OwnerSnapshotFetcher(
                exchange,
                credential,
                base_url,
                authorize=authorize,
                clock=clock,
                adapter_settings=adapter_settings,
                pager_budget=pager_budget,
                tenant_registry=tenant_registry,
                recorder=recorder,
            )
            for credential in credentials
        )

    async def __call__(
        self,
        tenant_id: TenantId,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult:
        _snapshot_params(operation_id, extra_params)
        for fetcher in self.fetchers:
            try:
                return await fetcher.fetch_for(tenant_id, operation_id, window, extra_params)
            except Exception:  # noqa: BLE001
                # An unavailable producer cannot disable the other verified tenant owners.
                # Every candidate independently completes authorization before fetching data.
                # nosec B112
                continue
        raise OwnerSnapshotUnavailableError()

    async def aclose(self) -> None:
        await asyncio.gather(
            *(fetcher.aclose() for fetcher in self.fetchers), return_exceptions=True
        )


def _configured_credentials(reference: SecretCredentialReference) -> tuple[str, ...]:
    try:
        raw = reference.get_secret_value().strip()
        if not raw:
            return ()
        if raw.startswith(("[", "{", '"')):
            values: object = json.loads(raw)
            if not isinstance(values, list):
                raise ValueError
            entries = cast(list[object], values)
            if any(not isinstance(value, str) or not value.strip() for value in entries):
                raise ValueError
            return tuple(dict.fromkeys(cast(str, value).strip() for value in entries))
        return (raw,)
    except Exception:  # noqa: BLE001 - never echo malformed secret JSON or parser diagnostics
        raise OwnerSnapshotConfigurationError() from None


def _snapshot_params(operation_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if operation_id not in {"GongziClQuery", "ScjdQuery"}:
        raise UnsupportedOperationError("operation is not a snapshot source")
    allowed: set[str] = {"Flag", "Type"} if operation_id == "GongziClQuery" else set()
    if any(key not in allowed for key in params):
        raise InvalidRequestError("snapshot requests cannot contain narrowing or grouping")
    if "Flag" in params:
        flag: object = params["Flag"]
        if not isinstance(flag, str) or flag not in {"0", "1"}:
            raise InvalidRequestError("snapshot wage flag is invalid")
    if "Type" in params:
        value: object = params["Type"]
        if not isinstance(value, str) or not value or not set(value.split(",")) <= {"0", "1", "2"}:
            raise InvalidRequestError("snapshot wage type is invalid")
    return dict(params)
