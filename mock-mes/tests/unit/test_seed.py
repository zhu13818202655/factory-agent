from __future__ import annotations

from decimal import Decimal

from mock_mes.seed import build_dataset


def test_small_dataset_hash_and_aggregates_are_stable() -> None:
    first = build_dataset("small", 20260821)
    second = build_dataset("small", 20260821)

    assert first.digest() == second.digest()
    assert first.digest() == "1c9c30427aeee08e0e6c6f1091aaa24b0735bba7a8054ce465e7644fb520d164"
    assert first.piecework_totals("tenant-a") == (Decimal("16"), Decimal("18.5000"))


def test_standard_dataset_is_seeded_and_extends_small() -> None:
    small = build_dataset("small", 20260821)
    standard = build_dataset("standard", 20260821)
    repeated = build_dataset("standard", 20260821)
    alternate = build_dataset("standard", 7)

    assert len(standard.resources["piecework_records"]) > len(small.resources["piecework_records"])
    assert standard.digest() == repeated.digest()
    assert standard.digest() != alternate.digest()


def test_identity_and_organization_edge_cases_are_present() -> None:
    dataset = build_dataset()
    memberships = dataset.memberships_by_subject["multi-tenant"]
    employees = dataset.resources["employees"]
    assignments = dataset.resources["organization_assignments"]

    assert {item["tenant_id"] for item in memberships} == {"tenant-a", "tenant-b"}
    assert len({item["employee_id"] for item in memberships}) == 2
    same_name = [item for item in employees if item["display_name"] == "Same Synthetic Name"]
    assert len(same_name) == 2
    assert next(item for item in employees if item["employee_id"] == "employee-a2")["dept_ids"] == [
        "group-a1",
        "group-a2",
    ]
    transferred = [item for item in assignments if item["employee_id"] == "employee-a3"]
    assert {item["valid_from"] for item in transferred} == {
        "2026-01-01T00:00:00Z",
        "2026-08-15T00:00:00Z",
    }


def test_production_and_payroll_edge_cases_are_present() -> None:
    dataset = build_dataset()
    piecework = dataset.resources["piecework_records"]
    operations = dataset.resources["operations"]
    plans = dataset.resources["production_plans"]
    orders = dataset.resources["orders"]
    settlements = dataset.resources["payroll_settlements"]

    assert {str(item["work_at"])[:7] for item in piecework if item["tenant_id"] == "tenant-a"} == {
        "2026-07",
        "2026-08",
    }
    assert {item["status"] for item in piecework} >= {"unsettled", "rework"}
    assert any(Decimal(str(item["defective_quantity"])) > 0 for item in piecework)
    parallel = [item for item in operations if item["tenant_id"] == "tenant-a"]
    assert len({item["sequence"] for item in parallel}) == 1
    assert any(item["planned_quantity"] == "0" for item in plans)
    assert any(item["status"] == "delayed" for item in orders)
    assert any(item["status"] == "draft" and item["published_at"] is None for item in settlements)
