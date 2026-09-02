"""Narrow user-supplied filters against the immutable DataScope.

Every rejection path here must happen before any MES business call.
"""

from __future__ import annotations

from factory_agent.domain import DataScope, DeptId, EmployeeId, NarrowedFilters

__all__ = [
    "FilterNarrower",
    "FilterRejectionError",
    "NarrowedFilters",
]


class FilterRejectionError(Exception):
    """Raised when a user filter cannot be proven inside the active scope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FilterNarrower:
    """Intersects user filters with the active DataScope; never broadens.

    ``employee_ids`` and ``dept_ids`` are intersected with the scope and an
    empty intersection is rejected before any business-data call.

    Business filters (``order_codes`` / ``style_codes`` /
    ``plan_codes`` and a user-requested department) are narrow-only: they are
    passed to MES which enforces row-level filtering (M3/M19), and a too-small
    return is surfaced via the M12 judgement. They are never treated as scope
    identifiers and can never broaden the scope.

    ``tenant_resolved_employee_ids`` are employees already resolved in the
    tenant through the MES-filtered ``EmployeeQuery`` (FR-012 target employee).
    Because that resolution itself obeys MES row-level filtering (a
    ``move_admin_role="00"`` caller only ever sees their own row), those ids
    enter the interaction with ``mes_filtered`` trust: MES decides actual
    visibility on the wage call, and an empty return surfaces as the M12
    "无权限或无数据" state. They are never accepted from raw user text.
    """

    def narrow(
        self,
        scope: DataScope,
        employee_ids: frozenset[EmployeeId] | None = None,
        dept_ids: frozenset[DeptId] | None = None,
        order_ids: frozenset[str] | None = None,
        style_ids: frozenset[str] | None = None,
        plan_ids: frozenset[str] | None = None,
        tenant_resolved_employee_ids: frozenset[EmployeeId] | None = None,
        *,
        restrict_to_scope_employees: bool = True,
    ) -> NarrowedFilters:
        narrowed_employees: frozenset[EmployeeId] | None
        if employee_ids is not None:
            narrowed_scope = scope.narrow_to_employees(employee_ids)
            if narrowed_scope is None:
                raise FilterRejectionError(
                    "forbidden",
                    "requested employees are outside the authorized scope",
                )
            narrowed_employees = narrowed_scope.employee_ids
        elif tenant_resolved_employee_ids is not None:
            narrowed_employees = tenant_resolved_employee_ids
        elif restrict_to_scope_employees:
            narrowed_employees = scope.employee_ids
        else:
            # Management/boss capabilities: no employee-level restriction on
            # our side; MES row-level filtering (M3/M19) decides the range.
            narrowed_employees = None

        requested_depts: frozenset[DeptId] | None = None
        narrowed_depts: frozenset[DeptId] | None
        if dept_ids is not None:
            narrowed_dept_scope = scope.narrow_to_depts(dept_ids)
            if narrowed_dept_scope is None:
                raise FilterRejectionError(
                    "forbidden",
                    "requested departments are outside the authorized scope",
                )
            narrowed_depts = narrowed_dept_scope.dept_ids
            requested_depts = narrowed_depts
        else:
            narrowed_depts = scope.dept_ids

        if employee_ids is not None and not narrowed_employees:
            raise FilterRejectionError("forbidden", "employee filter intersects to empty")
        if dept_ids is not None and not narrowed_depts:
            raise FilterRejectionError("forbidden", "department filter intersects to empty")

        return NarrowedFilters(
            tenant_id=scope.tenant_id,
            employee_ids=narrowed_employees,
            dept_ids=narrowed_depts,
            order_codes=_as_set(order_ids),
            style_codes=_as_set(style_ids),
            plan_codes=_as_set(plan_ids),
            requested_dept_ids=requested_depts,
        )


def _as_set(values: frozenset[str] | None) -> frozenset[str] | None:
    """Return None when empty so a recipe can distinguish "no filter"."""
    return values if values else None
