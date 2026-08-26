from __future__ import annotations

from mock_mes.config import MockMesSettings


def test_mock_settings_have_deterministic_offline_defaults() -> None:
    settings = MockMesSettings()

    assert settings.scenario == "small"
    assert settings.seed == 20260821
    assert settings.virtual_now.isoformat() == "2026-08-21T08:00:00+00:00"
