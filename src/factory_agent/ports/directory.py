"""Port over the MES-filtered department/employee directory.

``DeptRecord`` / ``EmployeeRecord`` are vendor-neutral directory records; the
``DirectoryResolver`` protocol lets application code resolve user business
filters (dept/employee names) without touching customer field names. Live
implementations live in ``data_api/`` and never leak customer payload shapes
past this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from factory_agent.domain import DataScope


@dataclass(frozen=True, slots=True)
class DeptRecord:
    """One MES-filtered department record used for name resolution."""

    dept_id: str
    name: str
    name_pk: str


@dataclass(frozen=True, slots=True)
class EmployeeRecord:
    """One MES-filtered employee record used for name resolution."""

    employee_id: str
    name: str
    name_pk: str


class DirectoryResolver(Protocol):
    """Port over the MES-filtered department/employee directory.

    Implementations live in ``data_api/`` and never leak customer field names
    past this boundary.
    """

    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]: ...

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]: ...


__all__ = [
    "DeptRecord",
    "DirectoryResolver",
    "EmployeeRecord",
]
