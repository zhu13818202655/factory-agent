"""Hongzhao MES adapter: the only place MES HTTP details are allowed.

URL construction, bearer credentials, raw payloads, and customer field names
are confined to this module. Application and domain code depend on the
``MesDataSource`` Protocol and never see base URLs or path mappings.

Story 5 semantics:
- Single ``HongzhaoMesAdapter``: POST + JSON, public parameter injection
  (app_key/timestamp/sign from ``MesCredentialBundle``), Bearer injection,
  ``{code, message, result}`` envelope unwrapping, ``footer`` extraction.
- Two-layer success judgment (M14): HTTP 200/404 first, then body ``code``
  1/0; failures are distinguished by ``message`` text only.
- Message → unified exception mapping: ``app_key不能为空``/``无效app_key``/
  ``加密信息解析失败`` → invalid_request; ``请求已过期``/``签名无效`` →
  unauthenticated (one refresh + one retry, never unbounded); HTTP 404 →
  upstream_unavailable (wrong endpoint); other ``code=0`` → upstream_invalid.
- accessToken refresh: proactive at a threshold (default 90 minutes of the
  2-hour validity, M2) plus exactly one reactive refresh-retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, cast

import httpx

from factory_agent.data_api.catalog import ApiCatalog, CatalogOperation
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.pagination import BoundedPager
from factory_agent.data_api.schemas import ROW_MODEL_BY_RESOURCE, row_to_plain_dict
from factory_agent.domain.errors import (
    InvalidRequestError,
    MesError,
    MesErrorCode,
    MesTimeoutError,
    RateLimitedError,
    TenantDisabledError,
    UnauthenticatedError,
    UnsupportedOperationError,
    UpstreamInvalidError,
    UpstreamUnavailableError,
)
from factory_agent.domain.queries import NarrowedFilters
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports import MesCallRecord, MesCallRecorder
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.tenant_registry import TenantRegistryReader

_LOGGER = get_logger("factory_agent.data_api.hongzhao")

#: Customer failure messages that indicate credential problems (M8/M14).
_EXPIRED_MESSAGES = ("请求已过期", "签名无效")
_INVALID_REQUEST_MESSAGES = (
    "app_key不能为空",
    "无效app_key",
    "加密信息解析失败",
)


class TokenRefresher(Protocol):
    """Port refreshing the credential bundle when it expires."""

    async def refresh(self) -> MesCredentialBundle: ...


@dataclass(frozen=True, slots=True)
class AdapterSettings:
    """Injected transport policy; conservative defaults for first release."""

    timeout_seconds: float = 10.0
    max_retries: int = 2
    default_retry_after_seconds: int = 1
    #: Proactive refresh threshold within the 2h token validity (M2).
    refresh_threshold_seconds: int = 5400


@dataclass(frozen=True, slots=True)
class MesRequest:
    """One approved operation plus its already-authorized JSON parameters."""

    operation_id: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MesResponse:
    """Unwrapped customer payload: result plus optional footer totals."""

    result: Any
    footer: dict[str, str] | None


def map_message_to_error(message: str) -> Exception:
    """Map a customer ``code=0`` message to the unified exception hierarchy."""
    for fragment in _EXPIRED_MESSAGES:
        if fragment in message:
            return UnauthenticatedError()
    for fragment in _INVALID_REQUEST_MESSAGES:
        if fragment in message:
            return InvalidRequestError("upstream rejected request parameters")
    return UpstreamInvalidError()


class HongzhaoMesAdapter:
    """HTTP adapter speaking the customer contract against Mock MES.

    A single process-level ``httpx.AsyncClient`` pool is reused across calls;
    timeouts and retry policy come from injected settings. The catalog is the
    operation whitelist: unregistered or disabled operations are rejected
    before any HTTP traffic.
    """

    def __init__(
        self,
        base_url: str,
        bundle: MesCredentialBundle,
        catalog: ApiCatalog,
        refresher: TokenRefresher | None = None,
        settings: AdapterSettings | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Any | None = None,
        pager: BoundedPager | None = None,
        recorder: MesCallRecorder | None = None,
        tenant_registry: TenantRegistryReader | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bundle = bundle
        self._catalog = catalog
        self._refresher = refresher
        self._settings = settings or AdapterSettings()
        self._client = client
        self._clock = clock
        self._pager = pager or BoundedPager(adapter=self)
        self._recorder = recorder
        self._tenant_registry = tenant_registry

    @property
    def bundle(self) -> MesCredentialBundle:
        return self._bundle

    async def set_bundle(self, bundle: MesCredentialBundle) -> None:
        object.__setattr__(self, "_bundle", bundle)

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._settings.timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def execute(self, request: MesRequest) -> MesResponse:
        """Execute one whitelisted operation with envelope unwrapping."""
        operation = self._operation(request.operation_id)
        await self._assert_tenant_enabled()
        if self._bundle.needs_refresh(self._now(), self._settings.refresh_threshold_seconds):
            await self._refresh_bundle()

        try:
            envelope = await self._send(operation, request.params)
        except UnauthenticatedError:
            # One refresh + one retry on expiry/signature failures (M1/M8).
            await self._refresh_bundle()
            envelope = await self._send(operation, request.params)
        if envelope.code == 1:
            if envelope.result is None:
                raise UpstreamInvalidError("successful response has no result")
            return self._unwrap(envelope)
        raise map_message_to_error(envelope.message)

    async def _assert_tenant_enabled(self) -> None:
        """D13 guard: a disabled AppKey is rejected before any MES request."""
        if self._tenant_registry is None:
            return
        record = await self._tenant_registry.get(self._bundle.app_key)
        if record is not None and record.status == "disabled":
            raise TenantDisabledError()

    async def fetch_resource_rows(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
    ) -> list[dict[str, Any]]:
        """Fetch validated customer rows; single-page path for the basic kernel."""
        fetched = await self.fetch_resource(operation_id, filters, time_range, page_size)
        return list(fetched.rows)

    async def fetch_resource(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
        extra_params: Mapping[str, str] | None = None,
    ) -> ResourceFetchResult:
        """Fetch every approved page with completeness proof and the optional footer.

        Extra params carry reviewed ``filter``-source parameters (e.g. a wage
        ``scheme``/``Flag``/``Type``); they can never carry scope or credential
        identifiers. The pager walks to ``result.total`` (M13) and returns a
        structured incompleteness reason on any anomaly rather than a truncated
        result.
        """
        operation = self._operation(operation_id)
        if operation.resource is None:
            raise InvalidRequestError("identity operations cannot fetch resource rows")
        item_model = ROW_MODEL_BY_RESOURCE.get(operation.resource)
        if item_model is None:
            raise UnsupportedOperationError("operation resource has no response model")
        params = self._resource_params(operation_id, filters, time_range, page_size, extra_params)
        paged = await self._pager.fetch_all(operation_id, params, item_model)
        return ResourceFetchResult(
            rows=tuple(row_to_plain_dict(row) for row in paged.items),
            total=paged.total,
            pages_fetched=paged.pages_fetched,
            complete=paged.complete,
            reason=paged.reason,
            footer=paged.footer,
        )

    def _resource_params(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
        extra_params: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """Build reviewed request parameters; scope IDs come only from filters."""
        start, end = time_range
        params: dict[str, Any] = {
            "dates": start.date().isoformat(),
            "datee": end.date().isoformat(),
        }
        if extra_params:
            params.update(extra_params)
        if operation_id in {
            "EmployeeQuery",
            "YskQuery",
            "GongziMxQuery",
            "WorktypeProgressQuery",
        }:
            if filters.employee_ids is not None and len(filters.employee_ids) == 1:
                employee_id = next(iter(filters.employee_ids))
                if operation_id == "EmployeeQuery":
                    params["uid"] = str(employee_id)
                elif operation_id == "WorktypeProgressQuery":
                    params["uid"] = str(employee_id)
                else:
                    params["Uid"] = str(employee_id)
        return params

    def _unwrap(self, envelope: Any) -> MesResponse:
        result = envelope.result
        footer: dict[str, str] | None = None
        if isinstance(result, dict):
            result_mapping = cast(dict[str, Any], result)
            raw_footer = result_mapping.get("footer")
            if isinstance(raw_footer, dict):
                footer_mapping = cast(dict[object, object], raw_footer)
                footer = {str(key): str(value) for key, value in footer_mapping.items()}
        return MesResponse(result=result, footer=footer)

    def _operation(self, operation_id: str) -> CatalogOperation:
        try:
            operation = self._catalog.get(operation_id)
        except UnsupportedOperationError:
            raise
        if not operation.enabled:
            # K7: registered but disabled operations are rejected pre-HTTP.
            raise UnsupportedOperationError("operation is disabled in this release")
        return operation

    async def _refresh_bundle(self) -> None:
        if self._refresher is None:
            raise UnauthenticatedError("credential expired and no refresher is configured")
        fresh = await self._refresher.refresh()
        await self.set_bundle(fresh)

    async def _send(self, operation: CatalogOperation, params: Mapping[str, Any]) -> Any:
        body = self._build_body(operation, params)
        attempts_left = self._settings.max_retries + 1
        last_error: Exception | None = None
        while attempts_left > 0:
            attempts_left -= 1
            started = self._now()
            try:
                response = await self._ensure_client().post(
                    operation.path,
                    json=body,
                    headers={"Authorization": f"Bearer {self._bundle.access_token}"},
                )
            except httpx.TimeoutException:
                last_error = MesTimeoutError()
                self._record_mes_call(operation, started, status="failed", error=last_error)
                continue
            except httpx.HTTPError:
                last_error = UpstreamUnavailableError("transport failure while calling upstream")
                self._record_mes_call(operation, started, status="failed", error=last_error)
                continue

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                error = RateLimitedError(retry_after_seconds=retry_after)
                self._record_mes_call(operation, started, status="failed", error=error)
                if attempts_left > 0:
                    last_error = error
                    continue
                raise error
            try:
                envelope = self._map_status(response)
            except Exception as error:  # noqa: BLE001 - failure is metered, then re-raised
                self._record_mes_call(operation, started, status="failed", error=error)
                raise
            self._record_mes_call(operation, started, status="completed", envelope=envelope)
            return envelope

        raise last_error or UpstreamUnavailableError("upstream call exhausted retries")

    def _record_mes_call(
        self,
        operation: CatalogOperation,
        started: datetime,
        *,
        status: str,
        envelope: Any | None = None,
        error: Exception | None = None,
    ) -> None:
        """Meter one MES HTTP attempt at its exit (Story 11 2.6).

        Success and failure are both recorded; the recorder never raises into
        the adapter — a recorder fault is alerted and dropped so the MES call
        result is never affected (Story 11 1.6 / R1). The record carries no
        URL, parameter value, or credential.
        """
        if self._recorder is None:
            return
        duration_ms = max(0, int((self._now() - started).total_seconds() * 1000))
        error_category = _error_category(error) if error is not None else None
        try:
            self._recorder.record(
                MesCallRecord(
                    operation_id=operation.operation_id,
                    page_count=1,
                    row_count=_result_row_count(envelope) if envelope is not None else 0,
                    duration_ms=duration_ms,
                    status="completed" if status == "completed" else "failed",
                    error_category=error_category,
                )
            )
        except Exception:  # noqa: BLE001 - metering must never break the MES call (R1)
            _LOGGER.exception(
                "mes.metering.record_failed",
                operation_id=operation.operation_id,
            )

    def _build_body(self, operation: CatalogOperation, params: Mapping[str, Any]) -> dict[str, Any]:
        """Inject public parameters from the bundle; reject unknown sources.

        Credential-sourced values come exclusively from ``MesCredentialBundle``
        — never from filters, user text, or model output.
        """
        body: dict[str, Any] = {}
        for parameter, source in operation.parameter_sources.items():
            if source == "credential":
                if parameter == "app_key":
                    body[parameter] = self._bundle.app_key
                elif parameter == "timestamp":
                    body[parameter] = self._bundle.timestamp
                elif parameter == "sign":
                    body[parameter] = self._bundle.sign
            else:
                value = params.get(parameter)
                if value is None and parameter in operation.required_params:
                    raise InvalidRequestError(f"missing required parameter: {parameter}")
                if value is not None:
                    body[parameter] = value
        for parameter in operation.required_params:
            if parameter not in body:
                raise InvalidRequestError(f"missing required parameter: {parameter}")
        return body

    def _map_status(self, response: httpx.Response) -> Any:
        from factory_agent.data_api.schemas import MesEnvelope

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as error:
                raise UpstreamInvalidError("response body is not valid JSON") from error
            try:
                return MesEnvelope.model_validate(payload)
            except Exception as error:  # noqa: BLE001 - pydantic ValidationError
                raise UpstreamInvalidError("envelope failed schema validation") from error
        if response.status_code == 404:
            # M14: wrong endpoint address surfaces as upstream_unavailable.
            raise UpstreamUnavailableError("upstream endpoint not found")
        if response.status_code == 400:
            raise InvalidRequestError("upstream rejected request parameters")
        if response.status_code == 503:
            raise UpstreamUnavailableError()
        raise UpstreamInvalidError(f"unexpected upstream status {response.status_code}")


def _parse_retry_after(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except ValueError:
        return None


def _error_category(error: Exception) -> str:
    """Canonical category for a metered failure; never carries message text."""
    if isinstance(error, MesError):
        return error.code.value
    return MesErrorCode.INTERNAL_ERROR.value


def _result_row_count(envelope: Any) -> int:
    """Row count estimate from a successful envelope's ``result``.

    ``mes_call_fact.row_count_bucket`` is derived from this count. The count
    is only ever an estimate for rollup display; call counts themselves come
    from event rows, never from summing this number (D6).
    """
    result = getattr(envelope, "result", None)
    if isinstance(result, list):
        return len(cast("list[object]", result))
    if isinstance(result, dict):
        payload = cast("dict[str, object]", result)
        rows = payload.get("list") or payload.get("rows")
        if isinstance(rows, list):
            return len(cast("list[object]", rows))
        total = payload.get("total")
        if isinstance(total, int):
            return total
    return 0


__all__ = [
    "AdapterSettings",
    "HongzhaoMesAdapter",
    "MesRequest",
    "MesResponse",
    "TokenRefresher",
    "map_message_to_error",
]
