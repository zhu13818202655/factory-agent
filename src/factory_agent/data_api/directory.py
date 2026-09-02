"""MES-backed directory and current-department membership.

Implements the application-layer ``OrganizationSource`` (K2 current
department membership) and ``DirectoryResolver`` (dept/employee name lookup)
over the reviewed ``DeptQuery`` / ``EmployeeQuery`` operations. Both are
MES-filtered by the Bearer identity (M3/M19): a ``move_admin_role="00"``
caller only ever resolves their own employee record.

Scope semantics: ``DataScope.dept_ids`` is the caller's *visible*
department range (DeptQuery, MES-filtered), not a single home department; a
wider range stays recorded as MES-side filtering (``mes_filtered``). User
department requests are intersected with this range by ``FilterNarrower``.
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
        """K2: the caller's current department range from DeptQuery/EmployeeQuery.

        ``move_admin_role="00"`` callers are own-data-only (M19): their scope is
        the single department of their own employee record. Other callers get
        the MES-filtered DeptQuery range (company/dept tiers), which is the
        range MES will actually return business rows for.
        """
        employee = await self._employee_by_id(tenant_id, employee_id)
        if employee is not None and employee.get("move_admin_role") == "00":
            own_dept = employee.get("dept")
            return (DeptId(str(own_dept)),) if own_dept else ()
        dept_rows = await self._dept_rows(tenant_id)
        return tuple(
            DeptId(str(row["id"]))
            for row in dept_rows
            if row.get("id") is not None and str(row["id"])
        )

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
