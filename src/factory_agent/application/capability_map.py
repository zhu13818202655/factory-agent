"""Product capability id (FR-001…) to recipe capability id mapping.

The permission matrix and the session authorizer work with the product-facing
``Capability`` enum values (FR-001…FR-012, 12 functions = employee 4 /
management 4 / boss 4, mapped 1:1 from the function tables in
``docs/product/需求及方案整理.md``). The capability runner executes reviewed
*recipes* with descriptive ids (e.g.
``fr002_personal_wage_summary``). This module is the single place that maps
between the two; a product id can own several recipes (FR-002/FR-003 share the
same operation with a different ``scheme``).
"""

from factory_agent.application.capabilities import CapabilityRegistry
from factory_agent.application.intent import CapabilityCatalog, CapabilitySpec
from factory_agent.application.permission_matrix import CAPABILITY_ROLES, Capability
from factory_agent.domain import CapabilityId

#: Recipe capability ids (execution) keyed by product capability id.
RECIPE_BY_FR: dict[str, str] = {
    "FR-001": "fr001_personal_output",
    "FR-002": "fr002_personal_wage_summary",
    "FR-003": "fr003_personal_wage_detail",
    "FR-004": "fr004_group_income_rank",
    "FR-005": "fr005_order_progress",
    "FR-006": "fr006_order_output",
    "FR-007": "fr007_workshop_output_comparison",
    "FR-008": "fr008_payroll_ranking",
    "FR-009": "fr009_factory_order_overview",
    "FR-010": "fr010_workshop_output_overview",
    "FR-011": "fr011_factory_payroll_stats",
    "FR-012": "fr012_employee_payroll",
    "FR-013": "fr013_factory_output_dashboard",
}

#: Sub-capabilities reached by drilling into a result rather than by the FR
#: function table. A drill-down is a *new* question at a finer grain ("这个订单的
#: 包级明细"), so it must be selectable, but it belongs to the same product
#: capability as the view it drills out of and therefore reuses that FR id for
#: the permission matrix. The FR level is not split (see 进度与产量功能重梳方案 §2).
DRILLDOWN_RECIPE_BY_FR: dict[str, tuple[str, ...]] = {
    "FR-005": ("fr005_order_package_detail", "fr005_order_worktype_detail"),
}

FR_BY_RECIPE: dict[str, str] = {
    **{recipe: fr for fr, recipe in RECIPE_BY_FR.items()},
    **{recipe: fr for fr, drilldowns in DRILLDOWN_RECIPE_BY_FR.items() for recipe in drilldowns},
}

#: Chinese title and one-line usage description per drill-down recipe. Separate
#: from ``FR_INFO`` because the FR table describes the entry-point capability,
#: not its finer-grain continuations.
DRILLDOWN_INFO: dict[str, tuple[str, str]] = {
    "fr005_order_package_detail": (
        "订单进度-包级明细",
        "查看某张订单每个包的裁剪数量、工序数、件·工序完成情况与完成率（需提供订单号）。",
    ),
    "fr005_order_worktype_detail": (
        "订单进度-工序明细",
        "查看某个包已刷卡的工序明细：预发数量、正品数量、操作人与刷卡时间（需提供物料编号）。",
    ),
}

#: Required slots per drill-down recipe, mirroring the recipe declarations.
DRILLDOWN_REQUIRED_SLOTS: dict[str, tuple[str, ...]] = {
    "fr005_order_package_detail": ("time_range", "order_codes"),
    "fr005_order_worktype_detail": ("time_range", "material_ids"),
}

#: Reserved capability id for non-business chit-chat. Never a recipe id and
#: never mapped through the permission matrix; the session pipeline intercepts
#: it before any capability authorization or MES call.
CHITCHAT_CAPABILITY_ID = "chitchat"

#: Chinese title and one-line usage description per product capability,
#: reviewed against the function tables in ``docs/product/需求及方案整理.md``
#: and the capability-role matrix in
#: ``docs/product/AI助手前端对接API文档.md`` (appendix A).
FR_INFO: dict[str, tuple[str, str]] = {
    "FR-001": (
        "个人产量统计",
        "按日期、款号、工序统计本人的计件产量（合格件数，次品不计）。",
    ),
    "FR-002": (
        "个人工资汇总（当日/当月）",
        "汇总本人选定时间段的计件工资合计、计件件数与日均工资。",
    ),
    "FR-003": (
        "个人工资明细",
        "查看本人选定时间段按日期、款号展开的工资明细（单价、数量、小计）。",
    ),
    "FR-004": (
        "收入排名（组内名次）",
        "查看本人在所属小组内的收入排名与对比。",
    ),
    "FR-005": (
        "订单/款号进度查询",
        "按订单号或款号查看生产进度：包完工率、包数、裁剪数量与工序数；可按订单继续下钻到包与工序。",
    ),
    "FR-006": (
        "订单/款号产量查询",
        "按订单号或款号查看各工序的报工产量、参与人数、报工金额与工序占比。",
    ),
    "FR-007": (
        "小组/车间产量对比",
        "查看管辖范围内各小组/车间之间的报工产量对比、名次与人均产量。",
    ),
    "FR-008": (
        "员工工资清单",
        "查看管辖范围内（组/车间）员工的计件件数与工资合计清单。",
    ),
    "FR-009": (
        "各订单进度（全厂总览）",
        "查看全厂订单的裁剪数量、包数与包完工率，列定义与订单进度一致。",
    ),
    "FR-010": (
        "小组/车间产量",
        "查看管辖范围内各小组/车间的报工产量，按款号展开并给出参与人数与人均产量。",
    ),
    "FR-011": (
        "全厂工资统计",
        "统计各车间/小组与全厂的工资应发合计、在册人数与人均工资。",
    ),
    "FR-012": (
        "员工工资查询（任一员工）",
        "查询某位员工选定时间段的工资合计、计件件数或工资明细。",
    ),
    "FR-013": (
        "全厂产量总览",
        "查看全厂总产量、在产订单数与人均产量的综合看板，含各车间产量排名与日产量趋势。",
    ),
}

#: Required intent slots per product capability. Optional business filters
#: (order/style/plan codes, dept names, employee names) are not required but
#: are accepted by the recipes that declare them.
REQUIRED_SLOTS_BY_FR: dict[str, tuple[str, ...]] = {
    "FR-001": ("time_range",),
    "FR-002": ("time_range",),
    "FR-003": ("time_range",),
    "FR-004": ("time_range",),
    "FR-005": ("time_range",),
    "FR-006": ("time_range",),
    "FR-007": ("time_range",),
    "FR-008": ("time_range",),
    "FR-009": ("time_range",),
    "FR-010": ("time_range",),
    "FR-011": ("time_range",),
    "FR-012": ("time_range", "employee_names"),
    "FR-013": ("time_range",),
}


def fr_id_for(recipe_id: str) -> str:
    """Map a recipe capability id to its product FR id (identity fallback)."""
    return FR_BY_RECIPE.get(recipe_id, recipe_id)


def recipe_id_for(fr_id: str) -> str:
    """Map a product FR id to its recipe capability id (identity fallback)."""
    return RECIPE_BY_FR.get(fr_id, fr_id)


def default_capability_catalog(
    registry: CapabilityRegistry | None = None,
) -> CapabilityCatalog:
    """Build the intent catalog from the reviewed recipe registry's capability ids.

    Product ids map to recipe ids 1:1 for selection; the parser returns the
    recipe id so the runner can execute it directly, and the authorizer maps it
    back to an FR id via ``fr_id_for``. ``registry`` is accepted for symmetry
    with ``CapabilityRegistry`` but the catalog is derived from the recipe map.

    Every business spec carries the roles of the reviewed capability-role
    matrix, so the selector prompt can be narrowed to one caller's own
    capabilities and the model never routes a request to a capability that
    role would be denied. Drill-down recipes are included — a drill-down is a
    new question — and inherit the roles of the FR id they share.
    """
    del registry  # the catalog is derived from the reviewed recipe map
    specs: list[CapabilitySpec] = []
    info: dict[str, tuple[str, str]] = {RECIPE_BY_FR[fr]: FR_INFO[fr] for fr in RECIPE_BY_FR}
    info.update(DRILLDOWN_INFO)
    slots: dict[str, tuple[str, ...]] = {
        RECIPE_BY_FR[fr]: REQUIRED_SLOTS_BY_FR.get(fr, ()) for fr in RECIPE_BY_FR
    }
    slots.update(DRILLDOWN_REQUIRED_SLOTS)

    for recipe in sorted(info):
        fr = FR_BY_RECIPE[recipe]
        title, description = info[recipe]
        specs.append(
            CapabilitySpec(
                capability_id=CapabilityId(recipe),
                title=title,
                description=description,
                required_slots=slots[recipe],
                roles=CAPABILITY_ROLES[Capability(fr)],
            )
        )
    specs.append(
        CapabilitySpec(
            capability_id=CapabilityId(CHITCHAT_CAPABILITY_ID),
            title="闲聊与常识问答",
            description=(
                "处理问候、寒暄以及与工厂业务无关的常识问答"
                "（如人物介绍、天气常识等），不查询任何工厂数据。"
            ),
            required_slots=(),
        )
    )
    return CapabilityCatalog(specs=tuple(specs))


__all__ = [
    "CHITCHAT_CAPABILITY_ID",
    "DRILLDOWN_INFO",
    "DRILLDOWN_RECIPE_BY_FR",
    "DRILLDOWN_REQUIRED_SLOTS",
    "FR_BY_RECIPE",
    "FR_INFO",
    "RECIPE_BY_FR",
    "REQUIRED_SLOTS_BY_FR",
    "default_capability_catalog",
    "fr_id_for",
    "recipe_id_for",
]
