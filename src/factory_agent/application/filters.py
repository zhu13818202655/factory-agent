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
    """Intersects user filters with the active DataScope; never broadens."""

    def narrow(
        self,
        scope: DataScope,
        employee_ids: frozenset[EmployeeId] | None = None,
        dept_ids: frozenset[DeptId] | None = None,
        order_ids: frozenset[str] | None = None,
        style_ids: frozenset[str] | None = None,
    ) -> NarrowedFilters:
        self._reject_unprovable_order_filters(order_ids, style_ids)

        narrowed_employees: frozenset[EmployeeId] | None
        if employee_ids is not None:
            narrowed_scope = scope.narrow_to_employees(employee_ids)
            if narrowed_scope is None:
                raise FilterRejectionError(
                    "forbidden",
                    "requested employees are outside the authorized scope",
                )
            narrowed_employees = narrowed_scope.employee_ids
        else:
            narrowed_employees = scope.employee_ids
        narrowed_depts: frozenset[DeptId] | None
        if dept_ids is not None:
            narrowed_dept_scope = scope.narrow_to_depts(dept_ids)
            if narrowed_dept_scope is None:
                raise FilterRejectionError(
                    "forbidden",
                    "requested departments are outside the authorized scope",
                )
            narrowed_depts = narrowed_dept_scope.dept_ids
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
        )

    @staticmethod
    def _reject_unprovable_order_filters(
        order_ids: frozenset[str] | None, style_ids: frozenset[str] | None
    ) -> None:
        """DEC-012 safe default: explicit order/style IDs cannot prove tenancy."""
        if order_ids or style_ids:
            raise FilterRejectionError(
                "invalid_request",
                "explicit order or style selection cannot be proven in the active tenant",
            )
