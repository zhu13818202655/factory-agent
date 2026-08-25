from __future__ import annotations

import pytest

from factory_agent.config import FactoryAgentSettings


def test_optional_services_are_disabled_by_default() -> None:
    settings = FactoryAgentSettings()

    assert settings.canonical_mes_base_url is None
    assert settings.postgres_url is None
    assert settings.redis_url is None
    assert settings.artifact_endpoint is None


def test_no_provider_url_or_key_is_configurable_here() -> None:
    """ADR-0006 keeps provider keys in the environment the registry names."""
    fields = set(FactoryAgentSettings.model_fields)

    assert "litellm_base_url" not in fields
    assert "litellm_api_key" not in fields
    assert FactoryAgentSettings().model_registry_path.name == "models.yaml"


def test_settings_read_unified_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_CANONICAL_MES_BASE_URL", "http://mock-mes:8010")
    monkeypatch.setenv("FACTORY_AGENT_POSTGRES_URL", "postgresql://secret@db/app")
    monkeypatch.setenv("FACTORY_AGENT_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("FACTORY_AGENT_ARTIFACT_ENDPOINT", "http://artifacts:9000")
    monkeypatch.setenv("FACTORY_AGENT_ARTIFACT_BUCKET", "exports")

    settings = FactoryAgentSettings()

    assert settings.canonical_mes_base_url is not None
    assert settings.postgres_url is not None
    assert settings.redis_url is not None
    assert settings.artifact_endpoint is not None
    assert settings.artifact_bucket == "exports"
