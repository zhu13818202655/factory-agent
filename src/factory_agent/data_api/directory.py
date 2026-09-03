"""MES-backed directory and current-department membership.

Implements the application-layer ``OrganizationSource`` (current department
membership) and ``DirectoryResolver`` (dept/employee name lookup) over the
reviewed ``DeptQuery`` / ``EmployeeQuery`` operations.

Base-data interfaces are role-independent: the customer returns the full roster
regardless of the caller (客户确认结论 4), so these lookups are never filtered
by the agent. The caller's *authoritative* role and bound departments come from
the token response and feed ``TenantContext``/``DataScope`` directly; this
source is only the degraded fallback for the minimal provable range, which is
the caller's own department from their own employee record. A wider range is
recorded as MES-side filtering (``mes_filtered``), never claimed here. User
department requests are intersected with the scope by ``FilterNarrower``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from factory_agent.domain import DataScope, DeptId, EmployeeId, NarrowedFilters, TenantId
from factory_agent.ports.directory import DeptRecord, EmployeeRecord

if TYPE_CHECKING:
    from factory_agent.data_api.catalog import ApiCatalog
    from factory_agent.data_api.hongzhao import HongzhaoMesAdapter

_WIDE_RANGE = (
    datetime(2000, 1, 1, tzinfo=timezone.utc),
    datetime(2100, 1, 1, tzinfo=timezone.utc),
)


class MesDirectorySource:
    """Directory + current-dept membership backed by the Hongzhao adapter."""

    def __init__(self, adapter: HongzhaoMesAdapter, catalog: ApiCatalog) -> None:
        self._adapter = adapter
        self._catalog = catalog

    # ------------------------------------------------------ OrganizationSource

    async def list_current_depts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> tuple[DeptId, ...]:
        """The caller's minimal provable department range (fallback only).

        The authoritative binding comes from the token response and is carried
        on ``TenantMembership.bound_dept_ids``; this fallback runs only when a
        bundle lacks a binding. Base-data queries are unfiltered (full roster),
        so the minimal provable range is the caller's own department read from
        their own employee record — never the whole roster.
        """
        employee = await self._employee_by_id(tenant_id, employee_id)
        if employee is None:
            return ()
        own_dept = employee.get("dept")
        return (DeptId(str(own_dept)),) if own_dept else ()

    # ------------------------------------------------------- DirectoryResolver

    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]:
        dept_rows = await self._dept_rows(scope.tenant_id)
        return tuple(
            DeptRecord(
                dept_id=str(row["id"]),
                name=str(row.get("name", "") or ""),
                name_pk=str(row.get("name_pk", "") or ""),
            )
            for row in dept_rows
            if row.get("id") is not None
        )

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]:
        rows = await self._adapter.fetch_resource_rows(
            "EmployeeQuery",
            NarrowedFilters(tenant_id=scope.tenant_id, employee_ids=None, dept_ids=None),
            _WIDE_RANGE,
            200,
        )
        return tuple(
            EmployeeRecord(
                employee_id=str(row["uid"]),
                name=str(row.get("uname", "") or ""),
                name_pk=str(row.get("name_pk", "") or ""),
            )
            for row in rows
            if row.get("uid") is not None
        )

    # ---------------------------------------------------------------- helpers

    async def _employee_by_id(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> dict[str, Any] | None:
        rows = await self._adapter.fetch_resource_rows(
            "EmployeeQuery",
            NarrowedFilters(
                tenant_id=tenant_id,
                employee_ids=frozenset({employee_id}),
                dept_ids=None,
            ),
            _WIDE_RANGE,
            200,
        )
        return rows[0] if rows else None

    async def _dept_rows(self, tenant_id: TenantId) -> list[dict[str, Any]]:
        return list(
            await self._adapter.fetch_resource_rows(
                "DeptQuery",
                NarrowedFilters(tenant_id=tenant_id, employee_ids=None, dept_ids=None),
                _WIDE_RANGE,
                200,
            )
        )


__all__ = ["MesDirectorySource"]
