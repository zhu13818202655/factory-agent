"""Deterministic dataset tests: hash stability and edge-case coverage."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from mock_mes.seed import build_dataset


def test_small_dataset_hash_is_stable() -> None:
    first = build_dataset("small", 20260821, datetime(2026, 8, 21, 8, tzinfo=timezone.utc))
    second = build_dataset("small", 20260821, datetime(2026, 8, 21, 8, tzinfo=timezone.utc))

    assert first.digest() == second.digest()


def test_standard_dataset_extends_small_and_depends_on_seed() -> None:
    small = build_dataset("small", 20260821)
    standard = build_dataset("standard", 20260821)
    repeated = build_dataset("standard", 20260821)
    alternate = build_dataset("standard", 7)

    assert len(standard.barcode_cl) > len(small.barcode_cl)
    assert standard.digest() == repeated.digest()
    assert standard.digest() != alternate.digest()


def test_wage_three_source_identity_holds() -> None:
    """je = sl × price must hold across all three wage sources (M9)."""
    dataset = build_dataset()

    for row in dataset.barcode_cl + dataset.dg_cl:
        quantity = row["sssl"] if "sssl" in row else row["sl"]
        assert Decimal(str(row["je"])) == Decimal(str(quantity)) * Decimal(str(row["price"]))
    for row in dataset.pin_feng:
        assert Decimal(str(row["je"])) == Decimal(str(row["sl"])) * Decimal(str(row["price"]))


def test_progress_consistency_between_barcodes_and_worktypes() -> None:
    """Scanned worktypes (uid non-empty) stay within the huohao worktype set."""
    dataset = build_dataset()
    known_worktypes = {str(row["wt"]) for row in dataset.huohao_worktypes}

    for row in dataset.barcodes:
        assert str(row["worktype"]) in known_worktypes
        assert row["uid"]  # a barcode record implies scanned


def test_organization_edge_cases_are_present() -> None:
    dataset = build_dataset()
    companies = {"COMPANY-A", "COMPANY-B"}
    assert {str(row["company"]) for row in dataset.depts} == companies
    # Single-level workshops only: no group tier (M5).
    assert all(row["pid"] == "0" for row in dataset.depts)
    # Same-name employees across workshops.
    names = [row["uname"] for row in dataset.employees]
    assert len(names) != len(set(names))
    # Own-data-only identity exists.
    roles = {str(row["move_admin_role"]) for row in dataset.employees}
    assert "00" in roles


def test_production_edge_cases_are_present() -> None:
    dataset = build_dataset()
    # Delayed order (finish_date before virtual_now) and zero plan exist.
    finish_dates = [str(row["finish_date"]) for row in dataset.plans]
    assert min(finish_dates) < "2026-08-21"
    assert any(str(row["zsl"]) == "0" for row in dataset.plans)
    # Defective quantity only appears in the manual-entry source (C.5).
    assert any(Decimal(str(row["cp"])) > 0 for row in dataset.pin_feng)
    # Cross-month scans.
    months = {str(row["rq"])[:7] for row in dataset.barcode_cl}
    assert months >= {"2026-07", "2026-08"}
