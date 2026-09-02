"""Deterministic generator tests (no database required).

``compute_day_rows`` is a pure function of ``(settings, day, prior_ssl)``; these
tests lock determinism, the business invariants, the work calendar, the
factory-scale organisation (headcount / roles / departments) and the
window-boundary behaviour without touching PostgreSQL.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from mock_mes.config import MockMesSettings
from mock_mes.generator.engine import compute_day_rows, day_digest, is_workday
from mock_mes.generator.fixtures import (
    ANCHOR_PLAN_1,
    ANCHOR_PLAN_2,
    ANCHOR_PLAN_3,
    ANCHOR_SCAN_1,
    ANCHOR_SCAN_2,
    ANCHOR_SCAN_3,
    ANCHOR_SCAN_4,
    ROLE_BOSS,
    ROLE_GROUP_LEADER,
    ROLE_MANAGER,
    ROLE_WORKER,
    master_rows,
)

SETTINGS = MockMesSettings()


def test_compute_day_rows_is_deterministic() -> None:
    day = date(2026, 8, 6)
    first = compute_day_rows(SETTINGS, day)
    second = compute_day_rows(SETTINGS, day)

    assert day_digest(first.inserts) == day_digest(second.inserts)
    assert [row.payload for row in first.inserts] == [row.payload for row in second.inserts]


def test_compute_day_rows_depends_on_seed_and_scale() -> None:
    day = date(2026, 8, 6)
    base = day_digest(compute_day_rows(SETTINGS, day).inserts)
    other_seed = day_digest(compute_day_rows(MockMesSettings(seed=SETTINGS.seed + 1), day).inserts)
    bigger = day_digest(compute_day_rows(MockMesSettings(headcount=1000), day).inserts)
    assert base != other_seed
    assert base != bigger


def test_wage_three_source_identity_holds() -> None:
    """je = sl × price must hold for every generated wage row (M9/M18)."""
    for day in (ANCHOR_SCAN_1, ANCHOR_SCAN_2, ANCHOR_SCAN_3, ANCHOR_SCAN_4, date(2026, 8, 14)):
        plan = compute_day_rows(SETTINGS, day)
        for row in plan.inserts:
            if row.table not in ("mock_barcode_cl", "mock_dg_cl", "mock_pin_feng"):
                continue
            quantity = row.payload.get("sssl", row.payload.get("sl"))
            assert Decimal(str(row.payload["je"])) == Decimal(str(quantity)) * Decimal(
                str(row.payload["price"])
            ), row.payload


def test_scan_worktypes_stay_within_known_set() -> None:
    """Scanned worktypes are always part of the worktype set (自洽)."""
    known = {"WT01", "WT02", "WT03"}
    for day in (ANCHOR_SCAN_1, ANCHOR_SCAN_3, ANCHOR_SCAN_4, date(2026, 8, 14)):
        # (anchored + rolling days)
        plan = compute_day_rows(SETTINGS, day)
        for row in plan.inserts:
            if row.table in ("mock_barcode", "mock_ysk"):
                assert row.payload["worktype"] in known
            if row.table == "mock_barcode":
                assert row.payload["uid"]  # a barcode record implies scanned


def test_anchored_fixtures_present_on_their_dates() -> None:
    """Anchored fixtures stay byte-identical on their anchor dates."""
    plan_1 = compute_day_rows(SETTINGS, ANCHOR_PLAN_1)
    assert any(
        r.table == "mock_plan" and r.payload["dh"] == "PLAN-2607-001" for r in plan_1.inserts
    )
    assert any(r.table == "mock_sclzd" and r.payload["id"] == "1001" for r in plan_1.inserts)

    scan_3 = compute_day_rows(SETTINGS, ANCHOR_SCAN_3)
    assert any(r.table == "mock_pin_feng" and r.payload["id"] == "pf-1" for r in scan_3.inserts)
    assert any(r.table == "mock_wsk" and r.payload["id"] == "1001" for r in scan_3.inserts)
    assert any(
        r.table == "mock_barcode_cl"
        and r.payload["id"] == "1001"
        and r.payload["rq"] == "2026-08-06"
        and r.payload["worktype"] == "WT03"
        and r.payload["sssl"] == "4"
        for r in scan_3.inserts
    )

    plan_2 = compute_day_rows(SETTINGS, ANCHOR_PLAN_2)
    assert any(
        r.table == "mock_sclzd" and r.payload["id"] == "1002" and r.payload["sssl"] == "3"
        for r in plan_2.inserts
    )
    assert any(r.table == "mock_plan" and r.payload["dh"] == "PLAN-B-001" for r in plan_2.inserts)

    plan_3 = compute_day_rows(SETTINGS, ANCHOR_PLAN_3)
    assert any(
        r.table == "mock_plan" and r.payload["zsl"] == "0" for r in plan_3.inserts
    )  # 零计划 edge case


def test_organisation_matches_factory_scale_and_roles() -> None:
    """Headcount, departments, and the four role tiers follow the settings."""
    settings = MockMesSettings(headcount=500, departments=5, group_size=10)
    rows = master_rows(settings)
    employees = [r.payload for r in rows if r.table == "mock_employee"]
    depts = [r.payload for r in rows if r.table == "mock_dept"]

    company_a = [e for e in employees if e["company"] == "COMPANY-A"]
    assert len(company_a) == 500
    assert len([d for d in depts if d["company"] == "COMPANY-A"]) == 5

    roles = {str(e["move_admin_role"]) for e in employees}
    assert roles == {ROLE_WORKER, ROLE_GROUP_LEADER, ROLE_MANAGER, ROLE_BOSS}
    # Exactly one boss per factory.
    assert len([e for e in employees if e["move_admin_role"] == ROLE_BOSS]) == 1
    # One manager per department.
    managers = [e for e in company_a if e["move_admin_role"] == ROLE_MANAGER]
    assert len(managers) == 5
    assert len({str(e["dept"]) for e in managers}) == 5
    # Several group leaders per department, roughly one per group.
    leaders = [e for e in company_a if e["move_admin_role"] == ROLE_GROUP_LEADER]
    assert len(leaders) >= 5 * (100 // 10) - 5
    # Tenant isolation still has a second company.
    assert {str(d["company"]) for d in depts} == {"COMPANY-A", "COMPANY-B"}
    # Same-name employees edge case survives (anchored 01001 / 01002).
    names = [str(e["uname"]) for e in employees]
    assert len(names) != len(set(names))
    # Master rows are deterministic across calls.
    assert [r.payload for r in master_rows(settings)] == [r.payload for r in rows]


def test_scale_parameters_change_the_organisation() -> None:
    small = MockMesSettings(headcount=100, departments=2, group_size=10)
    big = MockMesSettings(headcount=1000, departments=10, group_size=10)
    small_rows = [r for r in master_rows(small) if r.table == "mock_employee"]
    big_rows = [r for r in master_rows(big) if r.table == "mock_employee"]
    assert len(small_rows) < len(big_rows)
    assert len([r for r in small_rows if r.payload["company"] == "COMPANY-A"]) == 100


def test_work_calendar_is_deterministic() -> None:
    assert is_workday(date(2026, 8, 6))  # Thursday
    assert not is_workday(date(2026, 8, 8))  # Saturday
    assert not is_workday(date(2026, 10, 1))  # 国庆 holiday

    # No rolling output on non-production days.
    saturday = compute_day_rows(SETTINGS, date(2026, 8, 8))
    assert not [r for r in saturday.inserts if r.table == "mock_barcode_cl"]


def test_daily_output_matches_factory_scale() -> None:
    """A workday produces output for roughly active_ratio x headcount people."""
    plan = compute_day_rows(SETTINGS, date(2026, 8, 6))
    scans = [r for r in plan.inserts if r.table == "mock_barcode_cl"]
    assert len(scans) > 500  # ~80% of ~494 pieceworkers x 2 scans
    # Orders and their worktypes/unscanned rows are generated too.
    assert len([r for r in plan.inserts if r.table == "mock_plan"]) >= 3
    assert len([r for r in plan.inserts if r.table == "mock_sclzd_worktype"]) >= 9


def test_cross_day_ssl_accumulates_deterministically() -> None:
    """Rolling scans roll into the sclzd ssl; prior state is additive."""
    day = date(2026, 8, 6)
    first_totals = dict(compute_day_rows(SETTINGS, day).ssl_updates)
    assert first_totals, "expected rolling scans to accumulate order progress"
    detail_id, first_total = next(iter(first_totals.items()))
    assert first_total > 0

    later_totals = dict(compute_day_rows(SETTINGS, day, first_totals).ssl_updates)
    # The same order keeps accumulating: prior + today, never reset.
    assert later_totals[detail_id] == first_total * 2


def test_no_future_days_generated_by_config() -> None:
    """The data window is capped at virtual_now/today (never the future)."""
    settings = MockMesSettings(virtual_now=datetime(2026, 8, 21, 8, tzinfo=timezone.utc))
    assert settings.resolved_data_end == date(2026, 8, 21)

    # data_start defaults to January 1st of the previous year.
    assert settings.resolved_data_start.year == date.today().year - 1
    assert settings.resolved_data_start.month == 1 and settings.resolved_data_start.day == 1
