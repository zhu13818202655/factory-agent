"""Canonical MES adapter: the only place MES HTTP details are allowed.

URL construction, bearer credential pass-through, raw payloads, and customer
field names are confined to this module. Application and domain code depend on
the ``MesDataSource`` Protocol and never see base URLs or path mappings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, cast

import httpx
from pydantic import BaseModel, ValidationError

from factory_agent.data_api.schemas import (
    DepartmentResponse,
    EffectiveScopeResponse,
    EmployeeResponse,
    OperationResponse,
    OrderResponse,
    OrganizationAssignmentResponse,
    PayrollSettlementResponse,
    PieceworkRecordResponse,
    ProductionPlanResponse,
    StyleResponse,
    TenantMembershipResponse,
    row_to_plain_dict,
)
from factory_agent.domain.errors import (
    ForbiddenError,
    InternalError,
    InvalidRequestError,
    MesTimeoutError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    UpstreamInvalidError,
    UpstreamUnavailableError,
)
from factory_agent.domain.queries import (
    EffectiveScopeQuery,
    MembershipQuery,
    ResourceQuery,
)
from factory_agent.ports import MesDataSource

# Approved operation paths; anything unregistered is rejected before HTTP.
CANONICAL_OPERATION_PATHS: dict[str, str] = {
    "A1_getTenantMembership": "/v1/identity/memberships",
    "A2_listOrganizationAssignments": "/v1/organization-assignments",
    "A3_listEffectiveScopes": "/v1/effective-scopes",
    "C1_listPieceworkRecords": "/v1/piecework-records",
    "C2_listEmployees": "/v1/employees",
    "C3_listDepartments": "/v1/departments",
    "C4_listOrders": "/v1/orders",
    "C5_listStyles": "/v1/styles",
    "C6_listOperations": "/v1/operations",
    "C7_listProductionPlans": "/v1/production-plans",
    "C8_listPayrollSettlements": "/v1/payroll-settlements",
}

_RESOURCE_OPERATION: dict[str, str] = {
    "A2_listOrganizationAssignments": "organization_assignments",
    "C1_listPieceworkRecords": "piecework_records",
    "C2_listEmployees": "employees",
    "C3_listDepartments": "departments",
    "C4_listOrders": "orders",
    "C5_listStyles": "styles",
    "C6_listOperations": "operations",
    "C7_listProductionPlans": "production_plans",
    "C8_listPayrollSettlements": "payroll_settlements",
}


@dataclass(frozen=True, slots=True)
class AdapterSettings:
    """Injected transport policy; conservative defaults for first release."""

    timeout_seconds: float = 10.0
    max_retries: int = 2
    default_retry_after_seconds: int = 1


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """One approved operation plus its already-authorized query parameters."""

    operation_id: str
    query: tuple[tuple[str, str], ...]
    response_model: type[BaseModel]


def _encode_ids(ids: frozenset[Any] | None) -> str | None:
    if ids is None:
        return None
    values = sorted(str(item) for item in ids)
    if not values:
        return None
    return ",".join(values)


def resource_query_params(query: ResourceQuery) -> list[tuple[str, str]]:
    """Encode a scoped resource query into Canonical query parameters."""
    params: list[tuple[str, str]] = [
        ("X-Tenant-Id", str(query.tenant_id)),
        ("authorized_employee_ids", _encode_ids(query.employee_ids) or ""),
        ("authorized_dept_ids", _encode_ids(query.dept_ids) or ""),
        ("from", query.time_range.start.isoformat()),
        ("to", query.time_range.end.isoformat()),
        ("page", str(query.pagination.page)),
        ("size", str(query.pagination.size)),
    ]
    for name, value in (
        ("employee_ids", query.employee_ids),
        ("dept_ids", query.dept_ids),
        ("order_ids", query.order_ids),
        ("style_ids", query.style_ids),
        ("operation_ids", query.operation_ids),
        ("plan_ids", query.plan_ids),
        ("settlement_ids", query.settlement_ids),
    ):
        encoded = _encode_ids(value)
        if encoded:
            params.append((name, encoded))
    return params


def membership_query_params(query: MembershipQuery) -> list[tuple[str, str]]:
    return [("as_of", query.as_of.isoformat())]


def effective_scope_query_params(query: EffectiveScopeQuery) -> list[tuple[str, str]]:
    return [
        ("tenant_id", str(query.tenant_id)),
        ("as_of", query.as_of.isoformat()),
    ]


class CanonicalMesAdapter(MesDataSource[CanonicalRequest, BaseModel]):
    """HTTP adapter speaking the Canonical contract against Mock MES.

    A single process-level ``httpx.AsyncClient`` pool is reused across calls;
    timeouts and retry policy come from injected settings.
    """

    def __init__(
        self,
        base_url: str,
        credential: str,
        operation_paths: Mapping[str, str] | None = None,
        settings: AdapterSettings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._operation_paths = dict(operation_paths or CANONICAL_OPERATION_PATHS)
        self._settings = settings or AdapterSettings()
        self._client = client

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._settings.timeout_seconds,
                headers={"Authorization": f"Bearer {self._credential}"},
            )
        return self._client

    def _request_headers(self, params: dict[str, str]) -> dict[str, str]:
        """Credential and trusted tenant headers; never logged or persisted."""
        headers = {"Authorization": f"Bearer {self._credential}"}
        tenant_id = params.pop("X-Tenant-Id", None)
        if tenant_id is not None:
            headers["X-Tenant-Id"] = tenant_id
        return headers

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def execute(self, request: CanonicalRequest) -> BaseModel:
        try:
            path = self._operation_paths[request.operation_id]
        except KeyError as error:
            # Reject before any HTTP traffic.
            raise UnsupportedOperationError(
                "operation is not registered in the approved path whitelist"
            ) from error

        response_payload = await self._send(path, dict(request.query))
        try:
            return request.response_model.model_validate(response_payload)
        except ValidationError as error:
            raise UpstreamInvalidError(
                f"response failed schema validation for {request.operation_id}"
            ) from error

    async def _send(self, path: str, params: dict[str, str]) -> Any:
        attempts_left = self._settings.max_retries + 1
        last_error: Exception | None = None
        while attempts_left > 0:
            attempts_left -= 1
            try:
                response = await self._ensure_client().get(
                    path,
                    params=params,
                    headers=self._request_headers(params),
                )
            except httpx.TimeoutException:
                last_error = MesTimeoutError()
                continue
            except httpx.HTTPError:
                last_error = UpstreamUnavailableError("transport failure while calling upstream")
                continue

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                error = RateLimitedError(retry_after_seconds=retry_after)
                if attempts_left > 0:
                    last_error = error
                    continue
                raise error
            return self._map_status(response)

        raise last_error or UpstreamUnavailableError("upstream call exhausted retries")

    def _map_status(self, response: httpx.Response) -> Any:
        code = _error_code_of(response)
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as error:
                raise UpstreamInvalidError("response body is not valid JSON") from error
        if response.status_code == 400:
            raise InvalidRequestError(code or "upstream rejected request parameters")
        if response.status_code == 401:
            raise UnauthenticatedError()
        if response.status_code == 403:
            raise ForbiddenError()
        if response.status_code == 404:
            raise NotFoundError()
        if response.status_code == 409:
            raise InvalidRequestError("upstream reported a conflicting state")
        if response.status_code == 502:
            raise UpstreamInvalidError()
        if response.status_code in (503, 504):
            raise UpstreamUnavailableError()
        raise InternalError(f"unexpected upstream status {response.status_code}")


def _parse_retry_after(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except ValueError:
        return None


def _error_code_of(response: httpx.Response) -> str | None:
    try:
        body: object = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    mapping = cast("dict[str, object]", body)
    code = mapping.get("code")
    if isinstance(code, str):
        return code
    return None


# ---------------------------------------------------------------------------
# Typed fetch helpers used by the Story 3 execution kernel entry points.
# ---------------------------------------------------------------------------


async def fetch_membership(
    adapter: CanonicalMesAdapter, query: MembershipQuery
) -> TenantMembershipResponse:
    validated = await adapter.execute(
        CanonicalRequest(
            operation_id="A1_getTenantMembership",
            query=tuple(membership_query_params(query)),
            response_model=TenantMembershipResponse,
        )
    )
    if isinstance(validated, TenantMembershipResponse):
        return validated
    raise UpstreamInvalidError("membership payload failed validation")


async def fetch_effective_scopes(
    adapter: CanonicalMesAdapter, query: EffectiveScopeQuery
) -> tuple[EffectiveScopeResponse, ...]:
    from factory_agent.data_api.pagination import BoundedPager, PagerBudget

    pager = BoundedPager(adapter, budget=PagerBudget(max_pages=2, max_rows=400))
    result = await pager.fetch_all(
        operation_id="A3_listEffectiveScopes",
        base_params=effective_scope_query_params(query),
        item_model=EffectiveScopeResponse,
    )
    items = result.items
    return tuple(item for item in items if isinstance(item, EffectiveScopeResponse))


async def fetch_resource_rows(
    adapter: CanonicalMesAdapter, operation_id: str, query: ResourceQuery
) -> list[dict[str, Any]]:
    """Fetch one page of an authorized resource query as validated plain rows."""
    resource = _RESOURCE_OPERATION.get(operation_id)
    if resource is None:
        raise UnsupportedOperationError("operation is not a registered resource listing")
    item_model = _ITEM_MODEL_BY_RESOURCE[resource]
    page_model = _page_model(resource, item_model)
    validated = await adapter.execute(
        CanonicalRequest(
            operation_id=operation_id,
            query=tuple(resource_query_params(query)),
            response_model=page_model,
        )
    )
    rows: list[dict[str, Any]] = []
    for item in getattr(validated, "items"):
        raw = item.model_dump() if isinstance(item, BaseModel) else dict(item)
        revalidated = item_model.model_validate(raw)
        rows.append(row_to_plain_dict(revalidated))
    return rows


class FetchingAdapter(CanonicalMesAdapter):
    """Canonical adapter with the ``ResourceFetcher`` port method attached."""

    async def fetch_resource_rows(
        self, operation_id: str, query: ResourceQuery
    ) -> list[dict[str, Any]]:
        return await fetch_resource_rows(self, operation_id, query)


_ITEM_MODEL_BY_RESOURCE: dict[str, type[BaseModel]] = {
    "organization_assignments": OrganizationAssignmentResponse,
    "piecework_records": PieceworkRecordResponse,
    "employees": EmployeeResponse,
    "departments": DepartmentResponse,
    "orders": OrderResponse,
    "styles": StyleResponse,
    "operations": OperationResponse,
    "production_plans": ProductionPlanResponse,
    "payroll_settlements": PayrollSettlementResponse,
}

_PAGE_MODEL_BY_RESOURCE: dict[str, type[BaseModel]] = {}


def _page_model(resource: str, item_model: type[BaseModel]) -> type[BaseModel]:
    cached = _PAGE_MODEL_BY_RESOURCE.get(resource)
    if cached is not None:
        return cached

    class _Page(BaseModel):
        model_config = {"extra": "forbid"}
        items: list[item_model]  # type: ignore[valid-type]
        total: int
        page: int
        size: int

    _Page.__name__ = f"{resource.title().replace('_', '')}Page"
    _PAGE_MODEL_BY_RESOURCE[resource] = _Page
    return _Page


__all__ = [
    "AdapterSettings",
    "CANONICAL_OPERATION_PATHS",
    "CanonicalMesAdapter",
    "CanonicalRequest",
    "FetchingAdapter",
    "effective_scope_query_params",
    "fetch_effective_scopes",
    "fetch_membership",
    "fetch_resource_rows",
    "membership_query_params",
    "resource_query_params",
]
