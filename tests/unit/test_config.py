from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from factory_agent.config import DeployEnv, FactoryAgentSettings


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


# --- Environment tiers (ADR-0004 §Environment Tiers) -------------------------


def test_environment_defaults_to_prod() -> None:
    """An unconfigured deployment gets the strictest tier, not the loosest.

    "Safe by default" must not depend on anyone remembering to tighten it.
    """
    settings = FactoryAgentSettings()

    assert settings.environment == "prod"
    assert settings.is_developer_environment is False
    assert settings.debug_trace_active is False


@pytest.mark.parametrize("environment", ["cert", "staging", "prod"])
@pytest.mark.parametrize("debug_trace_enabled", [True, False])
def test_content_capture_is_unavailable_outside_developer_environments(
    environment: DeployEnv, debug_trace_enabled: bool
) -> None:
    """Acceptance and pre-production are grouped with production on purpose.

    ``cert`` and ``staging`` normally run against real customer data, so the
    debug switch there must be inert rather than merely "off by default" — a
    stray environment variable cannot widen the boundary.
    """
    settings = FactoryAgentSettings(
        environment=environment, debug_trace_enabled=debug_trace_enabled
    )

    assert settings.is_developer_environment is False
    assert settings.debug_trace_active is False


@pytest.mark.parametrize("environment", ["local", "dev"])
def test_content_capture_needs_both_the_environment_and_the_switch(environment: DeployEnv) -> None:
    allowed = FactoryAgentSettings(environment=environment, debug_trace_enabled=True)
    withheld = FactoryAgentSettings(environment=environment)

    assert allowed.debug_trace_active is True
    assert withheld.debug_trace_active is False


@pytest.mark.parametrize(
    ("written", "resolved"),
    [
        ("production", "prod"),
        ("development", "local"),
        ("stage", "staging"),
        ("PROD", "prod"),
        ("  dev  ", "dev"),
    ],
)
def test_environment_spellings_normalise(written: str, resolved: DeployEnv) -> None:
    """Legacy and colloquial spellings map onto the five canonical values.

    ``written`` stays a plain ``str`` on purpose — the before-validator is the
    subject of this test, and a spelling the static type already accepted would
    never reach it.
    """
    assert FactoryAgentSettings(environment=cast("DeployEnv", written)).environment == resolved


def test_ambiguous_test_environment_is_refused() -> None:
    """``test`` means CI to one caller and acceptance to another.

    Those two readings sit on opposite sides of the content-capture boundary, so
    there is nothing safe to infer: the operator is told to choose rather than
    handed a guess. Failing at startup is the only safe reading of a value that
    governs what may be captured.
    """
    with pytest.raises(ValidationError) as caught:
        # ``test`` sits outside DeployEnv by design — refusing it is the behaviour
        # under test, so the cast tells the checker what the runtime will reject.
        FactoryAgentSettings(environment=cast("DeployEnv", "test"))

    message = str(caught.value)
    assert "no equivalent" in message
    assert "local" in message
    assert "cert" in message


@pytest.mark.parametrize("environment", ["cert", "staging", "prod"])
def test_debug_log_level_is_withheld_outside_developer_environments(environment: DeployEnv) -> None:
    """DEBUG records carry far more of a payload than INFO ones (decision B)."""
    settings = FactoryAgentSettings(environment=environment, log_level="DEBUG")

    assert settings.effective_log_level == "INFO"


@pytest.mark.parametrize("environment", ["local", "dev"])
def test_debug_log_level_is_honoured_in_developer_environments(environment: DeployEnv) -> None:
    settings = FactoryAgentSettings(environment=environment, log_level="DEBUG")

    assert settings.effective_log_level == "DEBUG"


def test_environment_is_read_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_ENVIRONMENT", "dev")

    assert FactoryAgentSettings().environment == "dev"


def test_debug_trace_defaults_keep_capture_off_and_bounded() -> None:
    """The channel is off unless asked for, and bounded when it is asked for."""
    settings = FactoryAgentSettings()

    assert settings.debug_trace_enabled is False
    assert settings.debug_trace_retention_hours == 24
    assert settings.debug_trace_max_payload_bytes == 262_144
    assert settings.debug_trace_max_rows == 500


def test_third_party_log_floor_defaults_to_warning() -> None:
    assert FactoryAgentSettings().log_third_party_level == "WARNING"
