from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from usage_admin.config import get_settings
from usage_admin.migrations import build_alembic_config

_REPO_ROOT = Path(__file__).resolve().parents[3]


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


def _offline_sql(script_location: Path) -> str:
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost/db")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_usage_admin_migration_creates_only_its_owned_tables() -> None:
    """The migration directory must contain only the tables this service owns."""
    sql = _offline_sql(_REPO_ROOT / "usage-admin" / "migrations")

    assert "CREATE TABLE tenant_registry" in sql
    assert "CREATE TABLE platform_principal" in sql
    assert "CREATE TABLE admin_audit" in sql
    # factory-agent-owned business and MES metering tables are never created
    # here (legacy metering DDL from Story 8 is moved to factory-agent in
    # Story 11 and is out of scope for this Story).
    for foreign_table in (
        "agent_interaction",
        "agent_message",
        "mes_call_fact",
        "mes_operation_category",
    ):
        assert foreign_table not in sql


def test_new_tenant_migration_creates_only_story_nine_tables() -> None:
    """The new revision adds exactly tenant_registry and platform_principal."""
    migration = (
        _REPO_ROOT / "usage-admin" / "migrations" / "versions" / "20260829_0002_tenant_registry.py"
    ).read_text()
    assert 'op.create_table(\n        "tenant_registry"' in migration
    assert 'op.create_table(\n        "platform_principal"' in migration
    assert "mes_call_fact" not in migration
    assert "usage_event" not in migration


def test_factory_agent_migration_never_creates_shared_tables() -> None:
    """factory-agent's history must not create usage-admin-owned tables."""
    sql = _offline_sql(_REPO_ROOT / "migrations")

    for shared_table in ("tenant_registry", "platform_principal", "admin_audit"):
        assert shared_table not in sql
    assert "CREATE TABLE agent_interaction" in sql


def test_version_tables_are_isolated_between_services() -> None:
    """Each service pins its own Alembic version table (migration coexistence)."""
    usage_admin_env = (_REPO_ROOT / "usage-admin" / "migrations" / "env.py").read_text()
    factory_agent_env = (_REPO_ROOT / "migrations" / "env.py").read_text()

    assert "alembic_version_usage_admin" in usage_admin_env
    assert "alembic_version_factory_agent" in factory_agent_env
    assert "alembic_version_usage_admin" not in factory_agent_env
    assert "alembic_version_factory_agent" not in usage_admin_env
