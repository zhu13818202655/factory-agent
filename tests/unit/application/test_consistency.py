"""Role-consistency validator matrix (Story 2).

Covers: four-role × rules × strict/production dispositions. The core
guarantee the safety net must prove — a well-scoped return is never flagged
(zero false positives on the normal path) — is asserted here directly and in
the kernel-level tests in ``tests/security/``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from factory_agent.application.consistency import (
    ConsistencyValidator,
    ValidationAction,
    ValidationLevel,
)
from factory_agent.application.permission_matrix import Capability
from factory_agent.domain import (
    CapabilityId,
    DataScope,
    DeptId,
    EmployeeId,
    ExpectedRange,
    Role,
    ScopeVersion,
    TenantContext,
    TenantId,
    UserId,
)
from factory_agent.ports.session import CapabilityRunResult

VALIDATOR = ConsistencyValidator()
NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _ctx(role: Role, depts: tuple[str, ...] = ()) -> ExpectedRange:
    scope = DataScope(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId("01001")}),
        dept_ids=frozenset(DeptId(value) for value in depts),
        evaluated_at=NOW,
        scope_version=ScopeVersion("v1"),
    )
    context = TenantContext(
        tenant_id=TenantId("APPKEY-A"),
        user_id=UserId("u-1"),
        employee_id=EmployeeId("01001"),
        role=role,
        resolved_at=NOW,
    )
    return ExpectedRange.from_context(context, scope)


def _result(
    capability: str,
    *,
    columns: tuple[str, ...] = (),
    rows: tuple[tuple[object, ...], ...] = (),
    uids: tuple[str, ...] = (),
    depts: tuple[str, ...] = (),
) -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId(capability),
        column_names=columns,
        rows=rows,
        observed_uid_values=uids,
        observed_dept_values=depts,
    )


def test_employee_personal_wage_with_own_rows_is_ok() -> None:
    expected = _ctx(Role.EMPLOYEE, depts=("dept-a1",))
    verdict = VALIDATOR.validate(
        result=_result(
            "FR-002",
            columns=("gross_total",),
            rows=((1,),),
            uids=("01001",),
            depts=("dept-a1",),
        ),
        capability=Capability.OWN_PAYROLL_SUMMARY,
        expected=expected,
    )
    assert verdict.ok
    assert verdict.action is ValidationAction.PROCEED


def test_employee_personal_wage_with_other_employee_rows_is_exact_hit() -> None:
    """00 查他人工资场景：返回数据含非本人 uid → 精确命中，严格与生产均拦截."""
    expected = _ctx(Role.EMPLOYEE, depts=("dept-a1",))
    verdict = VALIDATOR.validate(
        result=_result(
            "FR-003",
            columns=("rq",),
            rows=(("2026-08-01",),),
            uids=("01001", "01002"),
            depts=("dept-a1",),
        ),
        capability=Capability.OWN_PAYROLL_DETAIL,
        expected=expected,
    )
    assert verdict.finding is not None
    assert verdict.finding.level is ValidationLevel.EXACT_HIT
    assert verdict.action is ValidationAction.BLOCK
    assert verdict.finding.sample_count == 1
    # No raw uid ever reaches the finding surface (only digests).
    assert "01002" not in verdict.finding.actual
    assert "01002" not in verdict.finding.reason


def test_manager_team_list_with_out_of_bound_dept_row_is_exact_hit() -> None:
    """01/02 查绑定范围外记录：输出行含绑定 dept 之外的 dept → 精确命中."""
    expected = _ctx(Role.MANAGER, depts=("dept-a2", "dept-a4"))
    verdict = VALIDATOR.validate(
        result=_result(
            "FR-008",
            columns=("uid", "uname", "dept", "gross"),
            rows=(("02001", "乙", "dept-b1", 1),),
            depts=("dept-a2", "dept-b1"),
        ),
        capability=Capability.TEAM_PAYROLL_LIST,
        expected=expected,
    )
    assert verdict.finding is not None
    assert verdict.finding.level is ValidationLevel.EXACT_HIT
    assert verdict.action is ValidationAction.BLOCK


def test_management_overview_observed_depts_within_bound_is_ok() -> None:
    expected = _ctx(Role.GROUP_LEADER, depts=("dept-a1",))
    verdict = VALIDATOR.validate(
        result=_result("FR-007", uids=("01001", "01012"), depts=("dept-a1",)),
        capability=Capability.WORKSHOP_COMPARISON,
        expected=expected,
    )
    assert verdict.ok


def test_owner_whole_factory_never_flagged_even_with_all_depts() -> None:
    """99 全厂聚合对角色无上限；即使覆盖所有 dept/uid 也不判异常."""
    expected = _ctx(Role.OWNER)
    for capability in (
        Capability.FACTORY_PAYROLL_STATS,
        Capability.WORKSHOP_OUTPUT_OVERVIEW,
        Capability.ANY_EMPLOYEE_PAYROLL,
    ):
        verdict = VALIDATOR.validate(
            result=_result(
                str(capability),
                columns=("dept_name",),
                rows=(("一车间",),),
                uids=("01001", "02001"),
                depts=("dept-a1", "dept-b1"),
            ),
            capability=capability,
            expected=expected,
        )
        assert verdict.ok, capability


def test_single_subject_multiple_rows_is_heuristic_and_mode_disposes() -> None:
    """单主体语义返回多行 → 启发式命中；严格模式阻塞、生产模式不拦截."""
    expected = _ctx(Role.EMPLOYEE, depts=("dept-a1",))
    result = _result(
        "FR-002",
        columns=("gross_total",),
        rows=((1,), (2,)),
        uids=("01001",),
    )

    strict = VALIDATOR.validate(
        result=result, capability=Capability.OWN_PAYROLL_SUMMARY, expected=expected, mode="strict"
    )
    production = VALIDATOR.validate(
        result=result,
        capability=Capability.OWN_PAYROLL_SUMMARY,
        expected=expected,
        mode="production",
    )
    assert strict.finding is not None
    assert strict.finding.level is ValidationLevel.HEURISTIC_HIT
    assert strict.action is ValidationAction.BLOCK
    assert production.finding is not None
    assert production.action is ValidationAction.PROCEED


def test_owner_single_total_row_is_a_business_fact_not_an_anomaly() -> None:
    """角色解释表：老板查全厂工资返回 1 条为业务事实，不判异常."""
    expected = _ctx(Role.OWNER)
    verdict = VALIDATOR.validate(
        result=_result(
            "FR-011",
            columns=("dept_name", "gross_total"),
            rows=(("汇总", 1),),
        ),
        capability=Capability.FACTORY_PAYROLL_STATS,
        expected=expected,
    )
    assert verdict.ok
