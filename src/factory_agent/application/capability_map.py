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

from __future__ import annotations

from factory_agent.application.capabilities import CapabilityRegistry
from factory_agent.application.intent import CapabilityCatalog, CapabilitySpec
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
}

FR_BY_RECIPE: dict[str, str] = {recipe: fr for fr, recipe in RECIPE_BY_FR.items()}

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
    """Build the intent catalog from the recipe registry's capability ids.

    Product ids map to recipe ids 1:1 for selection; the parser returns the
    recipe id so the runner can execute it directly, and the authorizer maps it
    back to an FR id via ``fr_id_for``. ``registry`` is accepted for symmetry
    with ``CapabilityRegistry`` but the catalog is derived from the recipe map.
    """
    del registry  # the catalog is derived from the reviewed recipe map
    specs = tuple(
        CapabilitySpec(
            capability_id=CapabilityId(recipe),
            title=fr,
            required_slots=REQUIRED_SLOTS_BY_FR.get(fr, ()),
        )
        for fr, recipe in sorted(RECIPE_BY_FR.items())
    )
    return CapabilityCatalog(specs=specs)


__all__ = [
    "FR_BY_RECIPE",
    "RECIPE_BY_FR",
    "REQUIRED_SLOTS_BY_FR",
    "default_capability_catalog",
    "fr_id_for",
    "recipe_id_for",
]
