"""Resolve user business filters against the directory.

``dept_names`` / ``employee_names`` / ``order_codes`` / ``style_codes`` /
``plan_codes`` are user *business* filters: they only narrow the requested
range and are enforced by MES-side row-level filtering plus the
"returned range smaller than requested" judgement. They can never broaden the
active ``DataScope``.

Directory lookups (``DeptQuery`` / ``EmployeeQuery``) return the full roster
regardless of role (客户确认结论 4), so a resolved target employee carries
``mes_filtered`` trust and the final wage/visibility call stays with the
customer MES.
"""

from __future__ import annotations

from dataclasses import dataclass

from factory_agent.domain import DataScope, DeptId, EmployeeId, IntentSlots
from factory_agent.ports.directory import DeptRecord, DirectoryResolver, EmployeeRecord


class DirectoryError(Exception):
    """A directory lookup failed: not found or ambiguous."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedBusinessFilters:
    """Narrowing inputs derived from user slots, ready for FilterNarrower."""

    employee_ids: frozenset[EmployeeId] | None
    dept_ids: frozenset[DeptId] | None
    order_codes: frozenset[str] | None
    style_codes: frozenset[str] | None
    plan_codes: frozenset[str] | None

    def is_empty(self) -> bool:
        return not any(
            (
                self.employee_ids,
                self.dept_ids,
                self.order_codes,
                self.style_codes,
                self.plan_codes,
            )
        )


class BusinessFilterResolver:
    """Turns intent slots into narrowed business filters via the directory."""

    def __init__(self, directory: DirectoryResolver) -> None:
        self._directory = directory

    async def resolve(self, scope: DataScope, slots: IntentSlots) -> ResolvedBusinessFilters:
        dept_ids = await self._resolve_dept_names(scope, slots.dept_names)
        employee_ids = await self._resolve_employee_names(scope, slots.employee_names)
        return ResolvedBusinessFilters(
            employee_ids=employee_ids,
            dept_ids=dept_ids,
            order_codes=_as_frozenset(slots.order_codes),
            style_codes=_as_frozenset(slots.style_codes),
            plan_codes=_as_frozenset(slots.plan_codes),
        )

    async def _resolve_dept_names(
        self, scope: DataScope, names: tuple[str, ...]
    ) -> frozenset[DeptId] | None:
        if not names:
            return None
        depts = await self._directory.list_depts(scope)
        resolved: set[DeptId] = set()
        for name in names:
            matches = [dept for dept in depts if dept.name == name or dept.name_pk == name]
            if not matches:
                raise DirectoryError("not_found", f"未找到车间「{name}」")
            distinct = {dept.dept_id for dept in matches}
            if len(distinct) > 1:
                raise DirectoryError("ambiguous", f"车间「{name}」存在多个匹配")
            resolved.add(DeptId(next(iter(distinct))))
        return frozenset(resolved)

    async def _resolve_employee_names(
        self, scope: DataScope, names: tuple[str, ...]
    ) -> frozenset[EmployeeId] | None:
        if not names:
            return None
        employees = await self._directory.list_employees(scope)
        resolved: set[EmployeeId] = set()
        for name in names:
            matches = [employee for employee in employees if employee.name == name]
            if not matches:
                raise DirectoryError("not_found", f"未找到员工「{name}」")
            distinct = {employee.employee_id for employee in matches}
            if len(distinct) > 1:
                raise DirectoryError("ambiguous", f"员工「{name}」存在同名，请提供工号")
            resolved.add(EmployeeId(next(iter(distinct))))
        return frozenset(resolved)


def _as_frozenset(values: tuple[str, ...]) -> frozenset[str] | None:
    return frozenset(values) if values else None


__all__ = [
    "BusinessFilterResolver",
    "DeptRecord",
    "DirectoryError",
    "DirectoryResolver",
    "EmployeeRecord",
    "ResolvedBusinessFilters",
]
