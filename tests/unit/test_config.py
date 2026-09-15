from pathlib import Path

import pytest

from factory_agent.config import FactoryAgentSettings


def test_optional_services_are_disabled_by_default() -> None:
    settings = FactoryAgentSettings()

    assert settings.canonical_mes_base_url is None
    assert settings.postgres_url is None
    assert settings.redis_url is None
    assert settings.export_retention_seconds == 7776000
    assert str(settings.export_store_dir) == str(Path("data/exports"))
    assert "artifact_endpoint" not in FactoryAgentSettings.model_fields


def test_s3_backend_is_off_until_an_endpoint_is_configured() -> None:
    """An empty endpoint keeps exports on the local directory backend."""
    settings = FactoryAgentSettings()

    assert settings.s3_endpoint_url == ""
    assert settings.s3_bucket == "factory-agent-exports"
    assert settings.s3_region == "us-east-1"
    assert settings.s3_path_style is True
    assert settings.s3_access_key.get_secret_value() == ""
    assert settings.s3_secret_key.get_secret_value() == ""


def test_no_provider_url_or_key_is_configurable_here() -> None:
    """ADR-0006 keeps provider keys in the environment the registry names."""
    fields = set(FactoryAgentSettings.model_fields)

    assert "litellm_base_url" not in fields
    assert "litellm_api_key" not in fields
    assert FactoryAgentSettings().model_registry_path.name == "models.yaml"


def test_settings_read_unified_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_CANONICAL_MES_BASE_URL", "http://mes-gateway:9002")
    monkeypatch.setenv("FACTORY_AGENT_POSTGRES_URL", "postgresql://secret@db/app")
    monkeypatch.setenv("FACTORY_AGENT_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("FACTORY_AGENT_EXPORT_RETENTION_SECONDS", "300")
    monkeypatch.setenv("FACTORY_AGENT_EXPORT_STORE_DIR", "/tmp/exports-alt")

    settings = FactoryAgentSettings()

    assert settings.canonical_mes_base_url is not None
    assert settings.postgres_url is not None
    assert settings.redis_url is not None
    assert settings.export_retention_seconds == 300
    assert str(settings.export_store_dir) == "/tmp/exports-alt"


def test_s3_settings_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_S3_ENDPOINT_URL", "http://127.0.0.1:8333")
    monkeypatch.setenv("FACTORY_AGENT_S3_BUCKET", "factory-agent-test")
    monkeypatch.setenv("FACTORY_AGENT_S3_ACCESS_KEY", "test-key")
    monkeypatch.setenv("FACTORY_AGENT_S3_SECRET_KEY", "test-secret")
    monkeypatch.setenv("FACTORY_AGENT_S3_PATH_STYLE", "false")

    settings = FactoryAgentSettings()

    assert settings.s3_endpoint_url == "http://127.0.0.1:8333"
    assert settings.s3_bucket == "factory-agent-test"
    assert settings.s3_path_style is False
    assert settings.s3_access_key.get_secret_value() == "test-key"
    assert settings.s3_secret_key.get_secret_value() == "test-secret"
    # Credentials must not surface in repr/str, only through an explicit read.
    assert "test-secret" not in repr(settings)
