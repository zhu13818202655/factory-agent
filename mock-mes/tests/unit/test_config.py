from __future__ import annotations

from mock_mes.config import MockMesSettings


def test_mock_settings_have_deterministic_offline_defaults() -> None:
    settings = MockMesSettings()

    assert settings.seed == 20260821
    assert settings.virtual_now.isoformat() == "2026-08-21T08:00:00+00:00"


def test_mock_settings_default_to_a_realistic_factory_scale() -> None:
    """Every scale parameter has a default (a ~500-person factory)."""
    settings = MockMesSettings()

    assert settings.headcount == 500
    assert settings.departments == 5
    assert settings.group_size == 10
    assert settings.headcount_secondary == 50
    assert settings.styles == 24
    assert settings.plans_per_day == 3
    assert 0 < settings.daily_active_ratio <= 1
    assert settings.scans_per_worker == 2
    assert settings.daily_hires == 0
    # One manager per department, one group leader per group.
    per_dept = settings.headcount // settings.departments
    assert per_dept == 100
    assert per_dept // settings.group_size == 10
