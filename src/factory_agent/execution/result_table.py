"""ResultTable and the versioned metric registry.

Every number in a ``ResultTable`` is traceable to its source operations and a
named, versioned metric. Temporary assumptions about unconfirmed business
formulas are registered explicitly instead of being silently baked in.
"""

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
                description="Personal output: sum of sl over the caller's own scanned rows",
                status="confirmed",
                assumption_status="客户已确认：个人产量即实收数（合格数），次品不计（客户确认结论）",
            ),
            # 报工产量的组织口径：与 ``output_personal`` 是同一个 ``sl`` 字段的两种
            # 观察面（个人 / 订单·款号·部门），口径一致但产品含义不同，故分别登记。
            MetricDefinition(
                name="output_reported_qty",
                version="customer-ysk-v1",
                description="Reported output: sum of sl over YskQuery detail rows",
                status="confirmed",
                assumption_status=(
                    "客户接口字段：生产查询-已扫描 sl（AI问答对外接口 §9.1，产量主数据源）；"
                    "件·工序口径，同一件衣服多道工序各计一条，不得当作完工件数"
                ),
            ),
            MetricDefinition(
                name="output_reported_amount",
                version="customer-ysk-v1",
                description="Reported output amount: sum of je over YskQuery detail rows",
                status="confirmed",
                assumption_status="客户接口字段：生产查询-已扫描 je（AI问答对外接口 §9.1）",
            ),
            MetricDefinition(
                name="output_worktype_share",
                version="factory-output-share-v1",
                description="One worktype's reported output over the order cut quantity",
                status="confirmed",
                assumption_status=(
                    "我方计算：工序占比 = 该工序报工产量 ÷ 裁剪数量（需求及方案整理·管理功能表）"
                ),
            ),
            MetricDefinition(
                name="progress_package_ratio",
                version="customer-progress-v1",
                description="Package completion ratio (wcl): finished packages over total packages",
                status="confirmed",
                assumption_status=(
                    "客户接口字段：缝制生产进度 wcl（= 完工包数 ÷ zbs，"
                    "实测 527/527 行反推为整数）；进度主列唯一口径，2026-09-15 拍板，"
                    "不使用数量口径完工率"
                ),
            ),
            MetricDefinition(
                name="progress_package_count",
                version="customer-progress-v1",
                description="Package count of one flowcard: ScjdQuery.zbs",
                status="confirmed",
                assumption_status=(
                    "客户接口字段：缝制生产进度 zbs（= 该单包数，实测 527/527 与包级行数一致）"
                ),
            ),
            MetricDefinition(
                name="progress_cut_qty",
                version="customer-progress-v1",
                description="Cut quantity: ScjdQuery.zsl / sum of package fhsl",
                status="confirmed",
                assumption_status=(
                    "客户接口字段：缝制生产进度 zsl（= 裁床数，实测 527/527 等于 Σ制单 fhsl）；"
                    "生产计划接口弃用后，原「计划数量」列改此口径并更名「裁剪数量」"
                ),
            ),
            MetricDefinition(
                name="progress_finished_packages",
                version="factory-progress-v1",
                description="Finished packages: round(wcl / 100 x zbs), or packages at wcl = 100",
                status="confirmed",
                assumption_status=(
                    "我方计算：完工包数由包完工率反推（列表层）或按包级 wcl == 100 计数（详情层）；"
                    "实测两条路逐单相等"
                ),
            ),
            MetricDefinition(
                name="progress_order_count",
                version="factory-progress-v1",
                description="Distinct flowcard count aggregated for one style",
                status="confirmed",
                assumption_status=(
                    "我方计算：款号视图的订单数 = COUNT(DISTINCT dh)（实测平均 7.03 单/款号）"
                ),
            ),
            MetricDefinition(
                name="progress_worktype_count",
                version="customer-progress-v1",
                description="Worktype count of one flowcard or package: wts",
                status="confirmed",
                assumption_status="客户接口字段：缝制生产进度/详情的 wts（工序道数，0 表示未开工）",
            ),
            MetricDefinition(
                name="progress_done_worktype_count",
                version="factory-progress-v1",
                description="Scanned worktype count of one package (distinct worktype rows)",
                status="confirmed",
                assumption_status=(
                    "我方计算：已完成工序数 = 缝制工序进度里 DISTINCT worktype 计数"
                    "（uid 非空即视为该道工序已完成）"
                ),
            ),
            MetricDefinition(
                name="progress_worktype_order",
                version="customer-progress-v1",
                description="Worktype sequence number inside the flowcard: wsort",
                status="confirmed",
                assumption_status="客户接口字段：缝制工序进度 wsort（工艺排序号）",
            ),
            MetricDefinition(
                name="progress_issued_qty",
                version="customer-progress-v1",
                description="Issued quantity of one worktype: fhsl",
                status="confirmed",
                assumption_status="客户接口字段：缝制工序进度 fhsl（该道工序预发数量）",
            ),
            MetricDefinition(
                name="progress_good_qty",
                version="customer-progress-v1",
                description="Good-piece quantity of one scanned worktype: zpsl",
                status="confirmed",
                assumption_status="客户接口字段：缝制工序进度 zpsl（正品数量）",
            ),
            MetricDefinition(
                name="progress_unit_total",
                version="customer-progress-v1",
                description="Package total units: wcs numerator, a piece-times-worktype figure",
                status="confirmed",
                assumption_status=(
                    "客户接口字段：缝制生产进度详情 wcs 前段；实测 = fhsl × wts，是件·工序口径，"
                    "不是件数，展示必须带口径注记"
                ),
            ),
            MetricDefinition(
                name="progress_unit_done",
                version="customer-progress-v1",
                description="Package finished units: wcs denominator",
                status="confirmed",
                assumption_status="客户接口字段：缝制生产进度详情 wcs 后段（完成数，件·工序口径）",
            ),
            MetricDefinition(
                name="output_participant_count",
                version="factory-participant-v1",
                description="Distinct uid with output in the window (factory-defined)",
                status="confirmed",
                assumption_status="我方定义：报工人数=uid 去重（需求及方案整理·管理功能表）",
            ),
            # 车间产量的口径已并入「报工产量」``output_reported_qty``（Σ Ysk.sl）：
            # 原 ``workshop_output_total``（BarcodeCl 语境的 sssl）实测是 Ysk 的**子集**、
            # 偏低约 51%，2026-09-15 随产量主源换源一并作废。
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
            # 交期族：生产计划接口弃用后（2026-09-15 拍板 3）再无数据源——制单的
            # khname / khid / dddh 与 ScjdQuery.dddh 实测全空。保留登记并把状态明确标为
            # ``unavailable``（与 ``plan_target_output`` 同法）：任何引用它的能力必须渲染
            # 不可用，不得输出数字；客户补新字段后再改回 confirmed。
            MetricDefinition(
                name="delivery_warning",
                version="factory-warning-v1",
                description="Delivery warning: no data source after the plan interface was dropped",
                status="unavailable",
                assumption_status=(
                    "客户现有接口不提供交期字段"
                    "（实测制单 khname/khid/dddh 与 ScjdQuery.dddh 全空）；等客户给出新字段再启用"
                ),
            ),
            MetricDefinition(
                name="delivery_days_remaining",
                version="factory-warning-v1",
                description="Days remaining until finish_date: no data source",
                status="unavailable",
                assumption_status="客户现有接口不提供交期字段；等客户给出新字段再启用",
            ),
            # 「当前工序 / 工序进度百分比」两个口径已从进度能力移除（2026-09-15 拍板 2：
            # 进度主列 = 包完工率）。工序层现在用 ``progress_done_worktype_count``（已完成
            # 工序数）与 ``progress_worktype_order``（工序顺序）表达，未做的工序只报「待做 N 道」。
            MetricDefinition(
                name="plan_target_output",
                version="unavailable-target-v1",
                description="Target output / achievement rate has no data source",
                status="unavailable",
                assumption_status=(
                    "客户明确现有接口不提供目标产量/达成率（需求及方案整理·追加确认 2026-09-15）；"
                    "任何引用该指标的能力必须渲染 unavailable，不得输出数字"
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
    #: Worker-facing Chinese display label; ``name`` stays the stable identifier.
    title: str | None = None


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
    #: Front-end card payload built by the kernel when the recipe declares a
    #: ``card:`` block; ``None`` = recipe has no card or the result is empty.
    card: dict[str, object] | None = None

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
