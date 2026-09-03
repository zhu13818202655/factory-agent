"""GongziMxQuery ``Uid`` rule: 传空查全部仅限老板角色（客户确认 2）.

The executor guard ``verify_payroll_uid_rule`` rejects a factory-wide wage
detail query for every role below boss *before* any MES traffic. Contract:
``docs/product/需求及方案整理.md``「客户确认结论」.
"""

from __future__ import annotations

import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.domain import EmployeeId, Role, TenantId
from factory_agent.domain.errors import ForbiddenError
from factory_agent.execution.executor import verify_payroll_uid_rule


def _filters(*, with_employee: bool) -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId("01001")}) if with_employee else None,
        dept_ids=None,
    )


def test_boss_may_query_all_workers_with_empty_uid() -> None:
    verify_payroll_uid_rule("GongziMxQuery", _filters(with_employee=False), Role.OWNER)


def test_manager_and_leader_empty_uid_is_rejected_before_http() -> None:
    for role in (Role.MANAGER, Role.GROUP_LEADER):
        with pytest.raises(ForbiddenError, match="仅限老板"):
            verify_payroll_uid_rule("GongziMxQuery", _filters(with_employee=False), role)


def test_worker_empty_uid_is_rejected() -> None:
    with pytest.raises(ForbiddenError, match="仅限老板"):
        verify_payroll_uid_rule("GongziMxQuery", _filters(with_employee=False), Role.EMPLOYEE)


def test_narrowed_uid_is_allowed_for_every_role() -> None:
    for role in (Role.OWNER, Role.MANAGER, Role.GROUP_LEADER, Role.EMPLOYEE):
        verify_payroll_uid_rule("GongziMxQuery", _filters(with_employee=True), role)


def test_other_operations_are_not_uid_gated() -> None:
    # The ranking interface has no Uid narrowing; only GongziMxQuery is gated.
    verify_payroll_uid_rule("GongziJeOrderQuery", _filters(with_employee=False), Role.EMPLOYEE)
    verify_payroll_uid_rule("BarcodeClQuery", _filters(with_employee=False), None)
