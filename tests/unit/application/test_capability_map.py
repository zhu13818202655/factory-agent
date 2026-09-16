"""The reviewed FR description source and the intent catalog derived from it.

The catalog feeds the capability-selector prompt, so every product capability
must carry a Chinese title and a one-line usage description, and the reserved
``chitchat`` entry must be present but never map to a recipe.
"""

from factory_agent.application.capability_map import (
    CHITCHAT_CAPABILITY_ID,
    DRILLDOWN_INFO,
    DRILLDOWN_RECIPE_BY_FR,
    FR_INFO,
    RECIPE_BY_FR,
    default_capability_catalog,
)
from factory_agent.domain import CapabilityId, Role


def _drilldown_recipes() -> list[str]:
    return [recipe for recipes in DRILLDOWN_RECIPE_BY_FR.values() for recipe in recipes]


def test_default_catalog_covers_every_recipe_plus_the_chitchat_entry() -> None:
    catalog = default_capability_catalog()

    ids = [str(spec.capability_id) for spec in catalog.specs]
    expected = sorted([*RECIPE_BY_FR.values(), *_drilldown_recipes(), CHITCHAT_CAPABILITY_ID])
    assert sorted(ids) == expected
    assert all(spec.title for spec in catalog.specs)
    assert all(spec.description for spec in catalog.specs)


def test_business_specs_match_the_reviewed_capability_info() -> None:
    """Entry-point specs come from FR_INFO; drill-downs have their own entries."""

    catalog = default_capability_catalog()
    info = {
        **{RECIPE_BY_FR[fr]: FR_INFO[fr] for fr in RECIPE_BY_FR},
        **DRILLDOWN_INFO,
    }

    for spec in catalog.specs:
        capability_id = str(spec.capability_id)
        if capability_id == CHITCHAT_CAPABILITY_ID:
            continue
        title, description = info[capability_id]
        assert spec.title == title
        assert spec.description == description
        assert spec.required_slots


def test_chitchat_spec_never_carries_required_slots() -> None:
    catalog = default_capability_catalog()

    spec = catalog.get(CHITCHAT_CAPABILITY_ID)
    assert spec is not None
    assert spec.capability_id == CapabilityId(CHITCHAT_CAPABILITY_ID)
    assert spec.required_slots == ()


def test_business_specs_carry_the_reviewed_role_sets() -> None:
    catalog = default_capability_catalog()

    workshop_output = catalog.get("fr010_workshop_output_overview")
    assert workshop_output is not None
    assert workshop_output.roles == frozenset({Role.GROUP_LEADER, Role.MANAGER, Role.OWNER})
    assert workshop_output.selectable_by(Role.GROUP_LEADER)
    assert workshop_output.selectable_by(Role.MANAGER)
    assert workshop_output.selectable_by(Role.OWNER)
    assert not workshop_output.selectable_by(Role.EMPLOYEE)

    owner_only = catalog.get("fr012_employee_payroll")
    assert owner_only is not None
    # D-5 (2026-09-16 拍板): 任一员工工资查询开放给组长/管理。
    assert owner_only.roles == frozenset({Role.GROUP_LEADER, Role.MANAGER, Role.OWNER})
    assert owner_only.selectable_by(Role.OWNER)
    assert owner_only.selectable_by(Role.MANAGER)
    assert not owner_only.selectable_by(Role.EMPLOYEE)

    dashboard = catalog.get("fr013_factory_output_dashboard")
    assert dashboard is not None
    assert dashboard.roles == frozenset({Role.OWNER})
    assert dashboard.selectable_by(Role.OWNER)
    assert not dashboard.selectable_by(Role.MANAGER)

    management = catalog.get("fr007_workshop_output_comparison")
    assert management is not None
    assert management.selectable_by(Role.MANAGER)
    assert management.selectable_by(Role.GROUP_LEADER)
    assert management.selectable_by(Role.OWNER)


def test_workshop_output_and_comparison_descriptions_stay_distinguishable() -> None:
    """只问产量时走 FR-010；FR-007 只在用户要对比/名次时命中.

    The two descriptions carry the whole distinction the selector model sees, so
    they must not converge on the same wording.
    """

    catalog = default_capability_catalog()

    comparison = catalog.get("fr007_workshop_output_comparison")
    overview = catalog.get("fr010_workshop_output_overview")
    assert comparison is not None
    assert overview is not None

    assert comparison.title == "小组/车间产量对比"
    assert "对比" in comparison.description
    assert "名次" in comparison.description

    assert overview.title == "小组/车间产量"
    assert "产量" in overview.description
    assert "对比" not in overview.description
    assert "名次" not in overview.description


def test_drilldown_specs_inherit_the_fr005_role_set() -> None:
    """下钻是新的提问，必须可选；但权限仍走 FR-005，不新增授权面."""

    catalog = default_capability_catalog()

    for recipe in _drilldown_recipes():
        spec = catalog.get(recipe)
        assert spec is not None, recipe
        assert spec.roles == frozenset({Role.GROUP_LEADER, Role.MANAGER, Role.OWNER})
        assert spec.selectable_by(Role.GROUP_LEADER)
        assert spec.selectable_by(Role.MANAGER)
        assert spec.selectable_by(Role.OWNER)
        assert not spec.selectable_by(Role.EMPLOYEE)
        assert recipe in catalog.describe(Role.OWNER)
        assert recipe not in catalog.describe(Role.EMPLOYEE)

    package = catalog.get("fr005_order_package_detail")
    worktype = catalog.get("fr005_order_worktype_detail")
    assert package is not None
    assert worktype is not None
    assert package.required_slots == ("time_range", "order_codes")
    assert worktype.required_slots == ("time_range", "material_ids")


def test_unknown_role_keeps_every_spec_selectable() -> None:
    """Offline callers pass no role and must see the whole catalog."""

    catalog = default_capability_catalog()

    assert all(spec.selectable_by(None) for spec in catalog.specs)
    assert set(catalog.describe(None).splitlines()) == {
        line for line in catalog.describe().splitlines()
    }


def test_describe_narrows_the_capability_list_to_the_caller_role() -> None:
    """The selector model never sees a capability its caller would be denied."""

    catalog = default_capability_catalog()

    employee = catalog.describe(Role.EMPLOYEE)
    assert "fr001_personal_output" in employee
    assert "fr005_order_progress" not in employee
    assert "fr010_workshop_output_overview" not in employee

    manager = catalog.describe(Role.MANAGER)
    assert "fr007_workshop_output_comparison" in manager
    assert "fr008_payroll_ranking" in manager
    assert "fr010_workshop_output_overview" in manager
    assert "fr009_factory_order_overview" not in manager
    assert "fr011_factory_payroll_stats" not in manager
    # D-5: fr012 开放给 01/02，选择器提示词必须出现（不变量反向：无权能力绝不出现）。
    assert "fr012_employee_payroll" in manager
    assert "fr013_factory_output_dashboard" not in manager

    owner = catalog.describe(Role.OWNER)
    for recipe in RECIPE_BY_FR.values():
        assert recipe in owner

    for role in Role:
        assert CHITCHAT_CAPABILITY_ID in catalog.describe(role)
