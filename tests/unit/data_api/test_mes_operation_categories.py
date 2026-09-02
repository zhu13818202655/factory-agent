"""``mes_operation_category`` consistency (Story 11 2.2 / 2.3).

The reviewed billing classification lives in ``configs/knowledge/apis.yaml``
(``usage_category``) and is mirrored as the seed rows of the single
development-baseline migration ``20260824_0001_session``. Any drift — a new
operation without a category, an illegal value, or a seed row out of sync with
the catalog — fails here so a new MES interface can never be added without
being classified (D5).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APIS_YAML = REPOSITORY_ROOT / "configs" / "knowledge" / "apis.yaml"
MIGRATION = REPOSITORY_ROOT / "migrations" / "versions" / "20260824_0001_session.py"

VALID_CATEGORIES = frozenset({"output", "payroll", "order", "other"})
EXPECTED_DISTRIBUTION = {"output": 6, "payroll": 2, "order": 4, "other": 15}


def load_catalog() -> dict[str, str]:
    document = yaml.safe_load(APIS_YAML.read_text(encoding="utf-8"))
    return {
        operation["operation_id"]: operation["usage_category"]
        for operation in document["operations"]
    }


def load_migration_seed() -> dict[str, str]:
    """Parse the (operation_id, category) tuples from the migration's INSERT."""
    source = MIGRATION.read_text(encoding="utf-8")
    tuples = re.findall(r"\('([A-Za-z0-9_]+)', '([a-z]+)', 'apis-v2'\)", source)
    assert tuples, "no seed tuples found in the migration"
    return dict(tuples)


def test_every_operation_has_a_legal_category_and_none_is_missing() -> None:
    categories = load_catalog()

    assert len(categories) == 27
    assert set(categories) == {
        "SystemToken",
        "QuerySign",
        "TestPermissions",
        "UserInfoQuery",
        "MoveMenuQuery",
        "HuohaoQuery",
        "HuohaoFormQuery",
        "ScTypeQuery",
        "RfidWorktypeQuery",
        "HuohaoWorktypeQuery",
        "EmployeeQuery",
        "DeptQuery",
        "PlanGridPageList",
        "SclzdGridPageList",
        "SclzdWorktypeQuery",
        "SclzdBarcodeQuery",
        "BarcodeClQuery",
        "HuohaoWtCLQuery",
        "PinFengGridPageList",
        "WorktypeProgressQuery",
        "YskQuery",
        "WskQuery",
        "GongziMxQuery",
        "GongziJeOrderQuery",
        "DgGridPageList",
        "DgZuGridPageList",
        "DgClQuery",
    }
    for operation_id, category in categories.items():
        assert category in VALID_CATEGORIES, (operation_id, category)


def test_category_distribution_matches_the_d5_review() -> None:
    from collections import Counter

    counts = Counter(load_catalog().values())

    assert dict(counts) == EXPECTED_DISTRIBUTION


def test_migration_seed_matches_the_apis_yaml_classification() -> None:
    catalog = load_catalog()
    seed = load_migration_seed()

    assert set(seed) == set(catalog), "every classified operation must be seeded"
    assert seed == catalog


def test_migration_seed_values_are_legal() -> None:
    seed = load_migration_seed()

    assert set(seed.values()) <= VALID_CATEGORIES
