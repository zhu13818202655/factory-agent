"""The reviewed FR description source and the intent catalog derived from it.

The catalog feeds the capability-selector prompt, so every product capability
must carry a Chinese title and a one-line usage description, and the reserved
``chitchat`` entry must be present but never map to a recipe.
"""

from __future__ import annotations

from factory_agent.application.capability_map import (
    CHITCHAT_CAPABILITY_ID,
    FR_INFO,
    RECIPE_BY_FR,
    default_capability_catalog,
)
from factory_agent.domain import CapabilityId


def test_default_catalog_covers_every_recipe_plus_the_chitchat_entry() -> None:
    catalog = default_capability_catalog()

    ids = [str(spec.capability_id) for spec in catalog.specs]
    expected = sorted([*RECIPE_BY_FR.values(), CHITCHAT_CAPABILITY_ID])
    assert sorted(ids) == expected
    assert all(spec.title for spec in catalog.specs)
    assert all(spec.description for spec in catalog.specs)


def test_business_specs_match_the_reviewed_fr_info() -> None:
    catalog = default_capability_catalog()
    fr_by_recipe = {recipe: fr for fr, recipe in RECIPE_BY_FR.items()}

    for spec in catalog.specs:
        if str(spec.capability_id) == CHITCHAT_CAPABILITY_ID:
            continue
        title, description = FR_INFO[fr_by_recipe[str(spec.capability_id)]]
        assert spec.title == title
        assert spec.description == description
        assert spec.required_slots


def test_chitchat_spec_never_carries_required_slots() -> None:
    catalog = default_capability_catalog()

    spec = catalog.get(CHITCHAT_CAPABILITY_ID)
    assert spec is not None
    assert spec.capability_id == CapabilityId(CHITCHAT_CAPABILITY_ID)
    assert spec.required_slots == ()
