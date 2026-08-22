from __future__ import annotations

import pytest

from factory_agent.config import get_settings
from factory_agent.execution.sandbox import InteractionSandboxPolicy
from factory_agent.persistence.migrations import build_alembic_config


def test_interaction_sandbox_policy_is_memory_only_and_read_only() -> None:
    policy = InteractionSandboxPolicy()

    assert policy.database == ":memory:"
    assert not policy.allow_external_access
    assert not policy.allow_unsigned_extensions
    assert not policy.allow_ddl
    assert not policy.allow_dml


def test_main_migrations_require_explicit_postgres_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACTORY_AGENT_POSTGRES_URL", raising=False)
    get_settings.cache_clear()

    with pytest.raises(SystemExit, match="FACTORY_AGENT_POSTGRES_URL is required"):
        build_alembic_config()


def test_main_migration_path_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_POSTGRES_URL", "postgresql://app:test@localhost/app")
    get_settings.cache_clear()

    config = build_alembic_config()

    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert script_location.endswith("factory-agent/migrations")
