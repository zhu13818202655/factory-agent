"""Execution kernel entry point with mandatory scope injection.

The executor is the only way application code reaches MES business data. It
accepts ``NarrowedFilters`` as the single scope exit, rejects
``PlatformScope``, and guarantees that out-of-scope IDs never enter adapter
parameters, sandbox tables, or logs.

Scope-derived parameters (``uid``/``Uid``) flow only from
``NarrowedFilters``; credential parameters (app_key/timestamp/sign) are
injected by the adapter from ``MesCredentialBundle`` and can never be supplied
through this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from factory_agent.domain import (
    DataScope,
    NarrowedFilters,
    PlatformScope,
    Role,
)
from factory_agent.domain.errors import ForbiddenError
from factory_agent.ports.contracts import ResourceFetchResult

#: Wage detail operation whose ``Uid`` may only be left empty by the boss role
#: (客户确认：GongziMxQuery 传空查全部仅限老板，管理传空不允许 — 见
#: ``docs/product/需求及方案整理.md``「客户确认结论」).
_GONGZI_MX_QUERY = "GongziMxQuery"


def verify_payroll_uid_rule(operation_id: str, filters: NarrowedFilters, role: Role | None) -> None:
    """Reject a whole-factory wage-detail query for every role below boss.

    An empty ``Uid`` (no narrowed employee) turns ``GongziMxQuery`` into a
    factory-wide wage query; only the owner role may do that. The check runs
    before any HTTP traffic.
    """
    if operation_id != _GONGZI_MX_QUERY:
        return
    if role is Role.OWNER:
        return
    if not filters.employee_ids:
        raise ForbiddenError("工资明细传空查全部仅限老板角色；管理角色请指定员工工号")


class ResourceFetcher(Protocol):
    """Port over validated resource fetching; implemented in ``data_api``."""

    async def fetch_resource_rows(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
    ) -> list[dict[str, Any]]: ...

    async def fetch_resource(
        self,
        operation_id: str,
        filters: NarrowedFilters,
        time_range: tuple[datetime, datetime],
        page_size: int,
        extra_params: Mapping[str, str] | None = None,
    ) -> ResourceFetchResult:
        """Fetch every page with completeness proof, footer, and any anomalies.

        ``extra_params`` carries reviewed filter parameters for the operation
        (e.g. a wage ``scheme``/``Flag``/``Type``); it can never carry scope or
        credential identifiers. A ``complete`` result proves the full range was
        retrieved up to ``result.total``.
        """
        ...


class CatalogReader(Protocol):
    """Port over the reviewed operation catalog; implemented in ``data_api``."""

    def get(self, operation_id: str) -> Any: ...


class ScopeVerifier(Protocol):
    """Proves the active scope matches the narrowed filters before any call."""

    def verify(self, scope: DataScope, filters: NarrowedFilters) -> None: ...


class StrictScopeVerifier:
    """Rejects any filter set not provably inside the active ``DataScope``."""

    def verify(self, scope: DataScope, filters: NarrowedFilters) -> None:
        if filters.tenant_id != scope.tenant_id:
            raise ForbiddenError("filter tenant does not match the active tenant")
        if filters.employee_ids is not None and not filters.employee_ids <= scope.employee_ids:
            raise ForbiddenError("employee filters exceed the authorized scope")
        if filters.dept_ids is not None and not filters.dept_ids <= scope.dept_ids:
            raise ForbiddenError("department filters exceed the authorized scope")


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """One bounded execution step requested by a capability recipe.

    ``role`` is the authoritative token role of the caller; it drives the
    reviewed payroll ``Uid`` rule and never broadens any scope.
    """

    operation_id: str
    time_range: tuple[datetime, datetime]
    pagination_size: int = 50
    role: Role | None = None


@dataclass(frozen=True, slots=True)
class StepResult:
    """Validated rows plus the operation provenance for traceability."""

    operation_id: str
    rows: tuple[dict[str, Any], ...]
    complete: bool


class ScopedExecutor:
    """Executes catalog-registered operations under the active DataScope.

    Scope parameters are taken exclusively from ``NarrowedFilters``; callers
    cannot inject employee/dept/tenant IDs through any other channel.
    """

    def __init__(
        self,
        adapter: ResourceFetcher,
        catalog: CatalogReader,
        verifier: ScopeVerifier | None = None,
    ) -> None:
        self._adapter = adapter
        self._catalog = catalog
        self._verifier = verifier or StrictScopeVerifier()

    async def execute_step(
        self,
        filters: NarrowedFilters,
        request: ExecutionRequest,
        active_scope: DataScope | None = None,
    ) -> StepResult:
        # Authorization completes before any business-data API call.
        self._catalog.get(request.operation_id)  # whitelist check before HTTP
        verify_payroll_uid_rule(request.operation_id, filters, request.role)
        if active_scope is not None:
            self._verifier.verify(active_scope, filters)

        rows = await self._adapter.fetch_resource_rows(
            request.operation_id,
            filters,
            request.time_range,
            request.pagination_size,
        )
        return StepResult(
            operation_id=request.operation_id,
            rows=tuple(rows),
            complete=True,
        )

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: ExecutionRequest,
        active_scope: DataScope | None = None,
        extra_params: Mapping[str, str] | None = None,
    ) -> ResourceFetchResult:
        """Execute one operation with completeness proof and the optional footer.

        Authorization completes before the business-data call; scope parameters
        come exclusively from ``filters`` and reviewed ``extra_params`` can only
        carry filter-sourced parameters.
        """
        self._catalog.get(request.operation_id)  # whitelist check before HTTP
        verify_payroll_uid_rule(request.operation_id, filters, request.role)
        if active_scope is not None:
            self._verifier.verify(active_scope, filters)
        return await self._adapter.fetch_resource(
            request.operation_id,
            filters,
            request.time_range,
            request.pagination_size,
            extra_params,
        )


def reject_platform_scope(scope: object) -> None:
    """Platform aggregation must never enter the MES execution path."""
    if isinstance(scope, PlatformScope):
        raise ForbiddenError("platform scope is not accepted by the MES executor")


__all__ = [
    "ExecutionRequest",
    "ScopedExecutor",
    "StepResult",
    "StrictScopeVerifier",
    "reject_platform_scope",
    "verify_payroll_uid_rule",
]
