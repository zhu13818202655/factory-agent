"""Narrow user-supplied filters against the immutable DataScope.

Every rejection path here must happen before any MES business call.
"""



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

    Exception — whole-tenant scopes (``scope.mes_filtered``, i.e. 99 老板): the
    local scope only carries the minimal provable range, so intersecting a
    factory-wide request against it would report the owner's own visible range
    as out of bounds. Such requests are passed through as narrow-only
    conditions for MES-side row filtering to answer.

    Business filters (``order_codes`` / ``style_codes`` /
    ``plan_codes`` / ``material_ids`` and a user-requested department) are
    narrow-only: they are
    passed to MES which enforces row-level filtering, and a too-small
    return is surfaced via the MES judgement. They are never treated as scope
    identifiers and can never broaden the scope.

    ``tenant_resolved_employee_ids`` are employees already resolved in the
    tenant through ``EmployeeQuery`` (FR-012 target employee). Those ids
    enter the interaction with ``mes_filtered`` trust: MES decides actual
    visibility on the wage call, and an empty return surfaces as the
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
        material_ids: frozenset[str] | None = None,
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
            # our side; MES row-level filtering decides the range.
            narrowed_employees = None

        requested_depts: frozenset[DeptId] | None = None
        narrowed_depts: frozenset[DeptId] | None
        if dept_ids is not None and scope.mes_filtered:
            # 99 老板：本地不持有部门全集（绑定部门只是最小可证范围），拿它做交集
            # 判定只会把工厂级的可见范围误判成越界。请求的部门本身是收窄条件
            # ——永不放大——所以原样交给 MES 行级过滤与配方本地 SQL 执行，
            # 由 MES 决定最终可见行。01/02/00 走下面的交集校验，一字不改。
            narrowed_depts = dept_ids
            requested_depts = dept_ids
        elif dept_ids is not None:
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
            material_ids=_as_set(material_ids),
            requested_dept_ids=requested_depts,
        )


def _as_set(values: frozenset[str] | None) -> frozenset[str] | None:
    """Return None when empty so a recipe can distinguish "no filter"."""
    return values if values else None
