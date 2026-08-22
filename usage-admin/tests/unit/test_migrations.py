from __future__ import annotations

import pytest
from usage_admin.config import get_settings
from usage_admin.migrations import build_alembic_config


def test_migration_config_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USAGE_ADMIN_DATABASE_URL", raising=False)
    get_settings.cache_clear()

    with pytest.raises(SystemExit, match="USAGE_ADMIN_DATABASE_URL is required"):
        build_alembic_config()


def test_migration_config_uses_configured_database(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = "postgresql+psycopg://usage:test@localhost/usage"
    monkeypatch.setenv("USAGE_ADMIN_DATABASE_URL", database_url)
    get_settings.cache_clear()

    config = build_alembic_config()

    assert config.get_main_option("sqlalchemy.url") == database_url
    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert script_location.endswith("usage-admin/migrations")
