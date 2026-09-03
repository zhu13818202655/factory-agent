"""Role-consistency validation safety net (Story 2).

Runs after a capability result returns and before anything user-visible is
composed (session orchestration step). It only judges and reports — it never
re-filters, re-scopes, rewrites rows, writes back, or triggers a re-fetch.
MES row-level filtering remains authoritative (``DataScope.mes_filtered``);
this layer is the *discovery mechanism* that surfaces an MES return which is
inconsistent with the expected role range, so the difference can be fed back
to the MES side.

Two tiers:

- Exact (``exact_hit``): the returned business rows carry ownership values
  (uid/dept) outside the ``ExpectedRange`` derived from the authoritative
  token role and binding. Conclusion is certain; in production it blocks the
  related rows from display with a friendly prompt plus an alert.
- Heuristic (``heuristic_hit``): the data shape disagrees with the role's
  expected subject semantics (e.g. a single-subject summary returning multiple
  rows). Conclusion is only a hint; it never blocks in production, is logged
  with full structured fields, and is periodically reviewed.

A role interpretation table guards every rule: the same shape can be a normal
business fact for one role and an anomaly for another (e.g. an owner asking a
whole-factory aggregate that returns a single total row is a business fact,
never an anomaly).

The declared row semantics per capability are data, reviewed like recipes:
changing a rule or a mapping is a semantics change requiring human review.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from factory_agent.application.permission_matrix import ROLE_DATA_RANGE, Capability
from factory_agent.domain import ExpectedRange, Role
from factory_agent.ports.session import CapabilityRunResult

#: Human-readable staged mode. strict = 对接期 (any inconsistency blocks the
#: result and is exposed as an integration problem for joint resolution with
#: the MES side); production = 主路径信任 MES + two-tier handling.
ValidationMode = StrEnum("ValidationMode", {"STRICT": "strict", "PRODUCTION": "production"})


class RowSemantic(StrEnum):
    """What a capability's result rows represent (declared, reviewed data)."""

    PERSONAL = "personal"
    """Detail rows of the caller's own records (FR-001 产量明细, FR-003 工资明细)."""

    PERSONAL_SUMMARY = "personal_summary"
    """A single summary row of the caller's own data (FR-002 工资汇总)."""

    GROUP_RANK = "group_rank"
    """The caller's own rank inside the bound group (FR-004). The underlying
    ranking fetch legitimately contains the visible group list, so only the
    dept dimension of that fetch is judged."""

    MANAGEMENT_OVERVIEW = "management_overview"
    """Dept/order/workshop aggregates for 01/02 (FR-005/006/007). No employee
    dimension is judged; dept values must stay inside the bound dept set."""

    TEAM_LIST = "team_list"
    """Rows are employees with uid+dept inside the bound group/dept (FR-008).
    dept is judged exactly (bound dept set); uid membership of a group cannot
    be proven locally and is never judged."""

    OWNER_OVERVIEW = "owner_overview"
    """Whole-factory aggregates for the 99 老板 role (FR-009/010/011/012).
    No range ceiling applies; a single total row is a business fact."""


@dataclass(frozen=True, slots=True)
class SemanticRule:
    """Reviewed judgement surface for one capability."""

    semantic: RowSemantic
    #: Judge the returned uid set against the caller's own record only.
    judge_uid_self: bool = False
    #: Judge returned/row-level dept values against the bound dept set.
    judge_dept: bool = False
    #: The output should describe exactly one subject (single summary row).
    single_subject: bool = False
    #: Where the semantic's rows are judged directly from a result column
    #: (e.g. FR-008 outputs uid/dept columns) — empty when output carries no
    #: ownership column and judgement relies on the kernel observation.
    output_uid_column: str | None = None
    output_dept_column: str | None = None


#: Reviewed capability → row-semantics mapping. Source of truth: the L1 recipe
#: result-column shapes in ``configs/knowledge/recipes/*.yaml`` cross-checked
#: against the capability-role matrix (``permission_matrix.py``).
ROW_SEMANTICS: dict[Capability, SemanticRule] = {
    Capability.OWN_OUTPUT: SemanticRule(RowSemantic.PERSONAL, judge_uid_self=True),
    Capability.OWN_PAYROLL_SUMMARY: SemanticRule(
        RowSemantic.PERSONAL_SUMMARY, judge_uid_self=True, single_subject=True
    ),
    Capability.OWN_PAYROLL_DETAIL: SemanticRule(RowSemantic.PERSONAL, judge_uid_self=True),
    Capability.GROUP_INCOME_RANK: SemanticRule(
        RowSemantic.GROUP_RANK, judge_dept=True, single_subject=True
    ),
    Capability.ORDER_PROGRESS: SemanticRule(RowSemantic.MANAGEMENT_OVERVIEW, judge_dept=True),
    Capability.ORDER_OUTPUT: SemanticRule(RowSemantic.MANAGEMENT_OVERVIEW, judge_dept=True),
    Capability.WORKSHOP_COMPARISON: SemanticRule(RowSemantic.MANAGEMENT_OVERVIEW, judge_dept=True),
    Capability.TEAM_PAYROLL_LIST: SemanticRule(
        RowSemantic.TEAM_LIST,
        judge_dept=True,
        output_uid_column="uid",
        output_dept_column="dept",
    ),
    Capability.FACTORY_ORDER_OVERVIEW: SemanticRule(RowSemantic.OWNER_OVERVIEW),
    Capability.WORKSHOP_OUTPUT_OVERVIEW: SemanticRule(RowSemantic.OWNER_OVERVIEW),
    Capability.FACTORY_PAYROLL_STATS: SemanticRule(RowSemantic.OWNER_OVERVIEW),
    Capability.ANY_EMPLOYEE_PAYROLL: SemanticRule(RowSemantic.OWNER_OVERVIEW, single_subject=True),
}

#: Role interpretation table. Documents why a data shape is normal for one
#: role and would be suspicious for another; heuristic rules consult it so the
#: same shape is never flagged for a role where it is a business fact.
ROLE_SHAPE_INTERPRETATION: dict[tuple[Role, RowSemantic], str] = {
    (
        Role.OWNER,
        RowSemantic.OWNER_OVERVIEW,
    ): "老板查全厂聚合返回 1 条（如全厂应发合计、全厂订单总数）是业务事实，不是异常",
    (
        Role.EMPLOYEE,
        RowSemantic.PERSONAL_SUMMARY,
    ): "员工查本人工资汇总返回 1 条为正常",
    (
        Role.GROUP_LEADER,
        RowSemantic.PERSONAL_SUMMARY,
    ): "组长查本人工资汇总返回 1 条为正常",
    (
        Role.MANAGER,
        RowSemantic.PERSONAL_SUMMARY,
    ): "管理查本人工资汇总返回 1 条为正常",
    (
        Role.EMPLOYEE,
        RowSemantic.GROUP_RANK,
    ): "员工查组内排名返回 1 条（本人名次）为正常",
}

#: Reviewed heuristic rules, registered as data with a reason. Kept deliberately
#: narrow: anything that cannot be decided without false positives stays out of
#: runtime and belongs to the periodic review instead.
_HEURISTIC_RULES: tuple[tuple[str, str], ...] = (
    (
        "single_subject_multiple_rows",
        "单主体语义（个人工资汇总/组内排名/任一员工工资）输出多于 1 行：真实 MES 上可能因"
        "一人多 uid/同名等业务原因出现，属可疑形态而非确证越权，按启发式记录不拦截。",
    ),
)


class ValidationLevel(StrEnum):
    OK = "ok"
    EXACT_HIT = "exact_hit"
    HEURISTIC_HIT = "heuristic_hit"


class ValidationAction(StrEnum):
    PROCEED = "proceed"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One consistency judgement result."""

    level: ValidationLevel
    code: str
    #: Human-readable friendly reason (可展示文案；不含范围外原始 id)。
    reason: str
    #: Readable expected range (角色可查范围描述，来自 Story 1 文案)。
    expected: str
    #: Readable actual observation (数量级 + 摘要，不含敏感原值)。
    actual: str
    sample_count: int = 0
    #: Irreversible digests of the offending values (minimal, non-sensitive).
    sample_digests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsistencyVerdict:
    """Validator output for one capability result."""

    finding: ValidationFinding | None
    action: ValidationAction

    @property
    def ok(self) -> bool:
        return self.finding is None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _range_text(expected: ExpectedRange) -> str:
    return ROLE_DATA_RANGE.get(expected.role, "当前角色可查范围")


def should_block(level: ValidationLevel, mode: str) -> bool:
    """Two-tier disposition: exact always blocks; heuristic blocks only in
    strict (对接期) mode where every inconsistency is an integration problem."""
    if level is ValidationLevel.EXACT_HIT:
        return True
    if level is ValidationLevel.HEURISTIC_HIT:
        return mode == ValidationMode.STRICT.value
    return False


class ConsistencyValidator:
    """Deterministic, read-only judge over a returned capability result.

    The validator never alters the result, never triggers a re-fetch, and never
    inspects anything but the declared row semantics plus the ownership values
    the kernel observed on the customer MES return.
    """

    def validate(
        self,
        *,
        result: CapabilityRunResult,
        capability: Capability,
        expected: ExpectedRange,
        mode: str = ValidationMode.STRICT.value,
    ) -> ConsistencyVerdict:
        rule = ROW_SEMANTICS.get(capability)
        if rule is None:
            return ConsistencyVerdict(finding=None, action=ValidationAction.PROCEED)

        exact = self._exact_finding(result, rule, expected)
        if exact is not None:
            return ConsistencyVerdict(
                finding=exact,
                action=(
                    ValidationAction.BLOCK
                    if should_block(ValidationLevel.EXACT_HIT, mode)
                    else ValidationAction.PROCEED
                ),
            )

        heuristic = self._heuristic_finding(result, rule, expected)
        if heuristic is not None:
            return ConsistencyVerdict(
                finding=heuristic,
                action=(
                    ValidationAction.BLOCK
                    if should_block(ValidationLevel.HEURISTIC_HIT, mode)
                    else ValidationAction.PROCEED
                ),
            )
        return ConsistencyVerdict(finding=None, action=ValidationAction.PROCEED)

    # ------------------------------------------------------------------
    # Exact checks.
    # ------------------------------------------------------------------

    def _exact_finding(
        self, result: CapabilityRunResult, rule: SemanticRule, expected: ExpectedRange
    ) -> ValidationFinding | None:
        offenders: list[str] = []
        dimension = ""

        if rule.judge_uid_self:
            out_of_range = [
                uid for uid in result.observed_uid_values if not expected.allows_employee(uid)
            ]
            if out_of_range:
                offenders = out_of_range
                dimension = "员工工号"

        if not offenders and rule.judge_dept and not expected.whole_tenant:
            out_of_range = [
                dept for dept in result.observed_dept_values if not expected.allows_dept(dept)
            ]
            if out_of_range:
                offenders = out_of_range
                dimension = "部门"

        if not offenders and rule.output_dept_column and not expected.whole_tenant:
            dept_index = _column_index(result, rule.output_dept_column)
            if dept_index is not None:
                out_of_range: list[str] = []
                for row in result.rows:
                    value = row[dept_index]
                    if value is None:
                        continue
                    if not expected.allows_dept(str(value)):
                        out_of_range.append(str(value))
                if out_of_range:
                    offenders = list(dict.fromkeys(out_of_range))
                    dimension = "部门"

        if not offenders or not dimension:
            return None

        digests = tuple(_digest(value) for value in offenders[:5])
        return ValidationFinding(
            level=ValidationLevel.EXACT_HIT,
            code="scope_mismatch_exact",
            reason=(
                "本次查询返回的数据中包含不在您可查范围内的记录，已停止展示相关数据。"
                f"您可查询的范围：{_range_text(expected)}。"
            ),
            expected=f"可查范围：{_range_text(expected)}",
            actual=(
                f"返回业务数据含 {len(offenders)} 个范围外{dimension}值"
                f"（已摘要：{','.join(digests)}）"
            ),
            sample_count=len(offenders),
            sample_digests=digests,
        )

    # ------------------------------------------------------------------
    # Heuristic checks.
    # ------------------------------------------------------------------

    def _heuristic_finding(
        self, result: CapabilityRunResult, rule: SemanticRule, expected: ExpectedRange
    ) -> ValidationFinding | None:
        if not rule.single_subject:
            return None
        if len(result.rows) <= 1:
            return None
        # Role interpretation: whole-factory aggregates for the owner may be a
        # single total row; any multi-row shape for other single-subject
        # semantics is only a hint (一人多 uid/同名 etc.), never a proven
        # violation — see the reviewed heuristic rule list.
        if expected.role is Role.OWNER and rule.semantic is RowSemantic.OWNER_OVERVIEW:
            return None
        return ValidationFinding(
            level=ValidationLevel.HEURISTIC_HIT,
            code=_HEURISTIC_RULES[0][0],
            reason=_HEURISTIC_RULES[0][1],
            expected=f"单主体语义，期望单行结果（可查范围：{_range_text(expected)}）",
            actual=f"实际返回 {len(result.rows)} 行结果",
            sample_count=len(result.rows),
        )


def _column_index(result: CapabilityRunResult, name: str) -> int | None:
    try:
        return result.column_names.index(name)
    except ValueError:
        return None


__all__ = [
    "ConsistencyValidator",
    "ConsistencyVerdict",
    "ROW_SEMANTICS",
    "ROLE_SHAPE_INTERPRETATION",
    "RowSemantic",
    "SemanticRule",
    "ValidationAction",
    "ValidationFinding",
    "ValidationLevel",
    "ValidationMode",
    "should_block",
]
