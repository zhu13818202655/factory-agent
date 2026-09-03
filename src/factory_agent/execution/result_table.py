"""ResultTable and the versioned metric registry.

Every number in a ``ResultTable`` is traceable to its source operations and a
named, versioned metric. Temporary assumptions about unconfirmed business
formulas are registered explicitly instead of being silently baked in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from factory_agent.domain.errors import InvalidRequestError
from factory_agent.ports.contracts import UNAVAILABLE_VALUE

#: ``UNAVAILABLE_VALUE`` is defined in ports.contracts and re-exported here for
#: callers that read it from the registry module.


class MetricDefinition(BaseModel):
    """One named metric with an explicit version and confirmation status.

    ``status`` follows the registry contract: ``confirmed`` metrics may
    participate in numeric computation; ``unconfirmed`` and ``unavailable``
    metrics must surface as an explicit ``unavailable`` column state instead of
    a fabricated number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    description: str
    status: Literal["confirmed", "unconfirmed", "unavailable"] = "unconfirmed"
    assumption_status: str = ""

    def allows_numeric_rendering(self) -> bool:
        return self.status == "confirmed"


class MetricRegistry:
    """Immutable registry of reviewed metrics; lookup requires name+version."""

    def __init__(self, metrics: tuple[MetricDefinition, ...] = ()) -> None:
        self._metrics: dict[tuple[str, str], MetricDefinition] = {}
        for metric in metrics:
            self._metrics[(metric.name, metric.version)] = metric

    def register(self, metric: MetricDefinition) -> None:
        self._metrics[(metric.name, metric.version)] = metric

    def resolve(self, name: str, version: str) -> MetricDefinition:
        try:
            return self._metrics[(name, version)]
        except KeyError as error:
            raise InvalidRequestError(f"metric is not registered: {name}@{version}") from error

    def has(self, name: str, version: str) -> bool:
        return (name, version) in self._metrics


def default_metric_registry() -> MetricRegistry:
    """Registry: customer-confirmed formulas plus explicit gaps.

    Confirmed metrics are grounded in ``docs/product/需求及方案整理.md``
    (功能表 + 客户确认结论) and ``docs/product/AI问答对外接口-整理.md``.
    Unconfirmed/unavailable entries have no confirmed data source and must
    never be rendered as numbers.
    """
    return MetricRegistry(
        metrics=(
            MetricDefinition(
                name="payroll_amount",
                version="customer-payroll-v1",
                description="Piecework wage per row: sl x price",
                status="confirmed",
                assumption_status="客户已确认：工资=数量×单价（需求及方案整理·实体对应关系）",
            ),
            MetricDefinition(
                name="payroll_gross_total",
                version="customer-footer-v1",
                description="Gross payable total from footer.je_total",
                status="confirmed",
                assumption_status="客户接口 footer.je_total 合计（AI问答对外接口 §8）",
            ),
            MetricDefinition(
                name="payroll_piece_count",
                version="customer-payroll-v1",
                description="Piecework quantity: sum of sl over the window",
                status="confirmed",
                assumption_status="客户已确认：个人产量即实收数（合格数），次品不计（客户确认结论）",
            ),
            MetricDefinition(
                name="payroll_unit_price",
                version="customer-payroll-v1",
                description="Piecework unit price for one wage detail row",
                status="confirmed",
                assumption_status="客户接口字段 price（AI问答对外接口 §8.1）",
            ),
            MetricDefinition(
                name="payroll_daily_average",
                version="factory-daily-average-v1",
                description="Daily average wage: gross total over natural days",
                status="confirmed",
                assumption_status="我方计算：日均工资=工资合计÷区间天数（需求及方案整理·员工功能表）",
            ),
            MetricDefinition(
                name="output_personal",
                version="customer-output-v1",
                description="Personal output: sum of sl in Ysk/BarcodeCl context",
                status="confirmed",
                assumption_status="客户已确认：个人产量即实收数（合格数），次品不计（客户确认结论）",
            ),
            MetricDefinition(
                name="output_order_completed",
                version="customer-completed-v1",
                description="Order completed quantity: sum of sssl (Sclzd context)",
                status="confirmed",
                assumption_status="客户接口字段 sssl 完工量（AI问答对外接口 §6.1）",
            ),
            MetricDefinition(
                name="output_order_plan_qty",
                version="customer-plan-v1",
                description="Order planned quantity: zsl/ddsl from Plan",
                status="confirmed",
                assumption_status="客户接口字段：订单数量（AI问答对外接口 §5.1）",
            ),
            MetricDefinition(
                name="output_order_in_progress",
                version="customer-wsk-v1",
                description="In-process quantity: WskQuery sl (待扫数量)",
                status="confirmed",
                assumption_status="客户接口字段：未扫描预发数量（AI问答对外接口 §9.2）",
            ),
            MetricDefinition(
                name="output_participant_count",
                version="factory-participant-v1",
                description="Distinct uid with output in the window (factory-defined)",
                status="confirmed",
                assumption_status="我方定义：报工人数=uid 去重（需求及方案整理·管理功能表）",
            ),
            MetricDefinition(
                name="workshop_output_total",
                version="customer-workshop-v1",
                description="Workshop output total: sum of output grouped by dept",
                status="confirmed",
                assumption_status="客户口径：产量按 dept 汇总；车间与部门平级（客户确认结论 1）",
            ),
            MetricDefinition(
                name="workshop_effective_headcount",
                version="factory-effective-headcount-v1",
                description="Effective headcount: distinct uid with output per dept",
                status="confirmed",
                assumption_status="我方定义：报工人数（uid 去重）；在册口径另见 org_headcount",
            ),
            MetricDefinition(
                name="workshop_output_per_capita",
                version="factory-per-capita-v1",
                description="Per-capita output: dept total over effective headcount",
                status="confirmed",
                assumption_status="我方计算：人均产量=总产量÷报工人数（需求及方案整理·管理功能表）",
            ),
            MetricDefinition(
                name="workshop_rank",
                version="factory-rank-v1",
                description="Workshop rank by total output, descending (1-based)",
                status="confirmed",
                assumption_status="我方计算：名次按总产量排序（需求及方案整理·管理功能表）",
            ),
            MetricDefinition(
                name="payroll_rank_position",
                version="factory-rank-v1",
                description="Income rank position over the visible ranked list",
                status="confirmed",
                assumption_status=(
                    "客户接口已按金额倒序返回（AI问答对外接口 §8.2；客户确认结论·接口与字段口径 3）"
                ),
            ),
            MetricDefinition(
                name="payroll_group_rank",
                version="factory-group-rank-v1",
                description="Caller's income rank inside their own group (dept)",
                status="confirmed",
                assumption_status=(
                    "客户确认：可见列表按 dept 过滤组内名次，无需额外数据"
                    "（客户确认结论·接口与字段口径 3）"
                ),
            ),
            MetricDefinition(
                name="payroll_group_size",
                version="factory-group-rank-v1",
                description="Total member count of the caller's group (dept)",
                status="confirmed",
                assumption_status=(
                    "客户确认：组内总人数=按 dept 过滤后的可见列表计数"
                    "（客户确认结论·接口与字段口径 3）"
                ),
            ),
            MetricDefinition(
                name="payroll_gross_by_dept",
                version="customer-footer-v1",
                description="Gross payable total grouped by dept (je total)",
                status="confirmed",
                assumption_status="客户接口字段 je 按 dept 汇总（AI问答对外接口 §8.2）",
            ),
            MetricDefinition(
                name="payroll_package_count",
                version="customer-rank-v1",
                description="Package count bs returned by GongziJeOrderQuery",
                status="confirmed",
                assumption_status="客户接口 bs 字段（AI问答对外接口 §8.2）",
            ),
            MetricDefinition(
                name="payroll_avg_by_dept",
                version="customer-payroll-avg-v1",
                description="Average wage by dept: gross total over registered headcount",
                status="confirmed",
                assumption_status=(
                    "客户口径：人均工资=应发合计÷在册人数（需求及方案整理·老板全厂工资取数）"
                ),
            ),
            MetricDefinition(
                name="delivery_warning",
                version="factory-warning-v1",
                description="Delivery warning: unfinished and days-to-delivery within threshold",
                status="confirmed",
                assumption_status=(
                    "预警=未完工且距交期剩余天数≤阈值；阈值默认 "
                    "max(1,⌈总工期×10%⌉)，回退固定 7 天（Story 3 双跑复核）"
                ),
            ),
            MetricDefinition(
                name="delivery_days_remaining",
                version="factory-warning-v1",
                description="Days remaining until finish_date (negative when overdue)",
                status="confirmed",
                assumption_status="剩余天数=交期−今日（需求及方案整理·老板功能表 交期预警）",
            ),
            MetricDefinition(
                name="worktype_current",
                version="customer-progress-v1",
                description="Current worktype: next after max completed wsort",
                status="confirmed",
                assumption_status="客户口径：当前工序=最大已完成工序的下一道（需求及方案整理·管理取数方式）",
            ),
            MetricDefinition(
                name="quality_defective",
                version="unavailable-defective-v1",
                description="Defective quantity: manual-entry cp only, no unified source",
                status="unavailable",
                assumption_status="无统一数据源：次品字段仅手工账接口提供（需求及方案整理·未直接映射接口表）",
            ),
            MetricDefinition(
                name="progress_ratio",
                version="customer-progress-v1",
                description="Scanned worktype count over total worktype count",
                status="confirmed",
                assumption_status="客户口径：工序进度按已完成工序数占比（需求及方案整理·管理取数方式）",
            ),
            MetricDefinition(
                name="plan_target_output",
                version="unavailable-target-v1",
                description="Target output / achievement rate has no data source",
                status="unavailable",
                assumption_status=(
                    "无数据源：计划达成率口径未确认（需求及方案整理 功能表未给出取数）"
                ),
            ),
            MetricDefinition(
                name="org_headcount",
                version="employee-registered-v1",
                description="Registered headcount from EmployeeQuery full roster",
                status="confirmed",
                assumption_status=(
                    "客户确认：基础数据接口不按权限过滤、返回全部（客户确认结论 4）；"
                    "在职/离职字段待联调复核"
                ),
            ),
            MetricDefinition(
                name="time_flag_default",
                version="confirmed-flag-v1",
                description="GongziMxQuery Flag default: 0 scan date / 1 review date",
                status="confirmed",
                assumption_status=(
                    "客户口径：Flag 0 按扫描日期 / 1 按审核日期，默认 0"
                    "（需求及方案整理·公共参数约定）"
                ),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ResultColumnMeta:
    name: str
    metric_name: str | None
    metric_version: str | None
    source_operations: tuple[str, ...]
    column_type: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class ResultTable:
    """Typed result with full provenance for every number."""

    capability_id: str
    columns: tuple[ResultColumnMeta, ...]
    rows: tuple[dict[str, Any], ...]
    totals: dict[str, Decimal]
    source_operations: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    incomplete: bool = False
    incomplete_reason: str | None = None

    def trace_for(self, column_name: str) -> ResultColumnMeta:
        for column in self.columns:
            if column.name == column_name:
                if column.metric_name is None or column.metric_version is None:
                    raise InvalidRequestError(f"column {column_name} lacks metric provenance")
                return column
        raise InvalidRequestError(f"unknown column: {column_name}")


__all__ = [
    "UNAVAILABLE_VALUE",
    "MetricDefinition",
    "MetricRegistry",
    "ResultColumnMeta",
    "ResultTable",
    "default_metric_registry",
]
