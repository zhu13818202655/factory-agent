from __future__ import annotations

from pathlib import Path

import pytest

from factory_agent.domain.errors import MesError
from factory_agent.llm.registry import DEFAULT_MODELS_PATH, load_model_registry

CANARY_KEY = "sk-canary-must-not-appear"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


VALID = """
version: 1
aliases:
  - alias: factory-fast
    fallbacks: [factory-reasoning]
    deployments:
      - model: deepseek/deepseek-chat
        api_base: https://api.deepseek.com/v1
        api_key_env: KEY_A
        priority: 1
      - model: openai/gpt-4o-mini
        api_base: https://api.openai.com/v1
        api_key_env: KEY_B
        priority: 2
  - alias: factory-reasoning
    deployments:
      - model: deepseek/deepseek-reasoner
        api_base: https://api.deepseek.com/v1
        api_key_env: KEY_A
"""


def test_the_shipped_registry_is_valid() -> None:
    registry = load_model_registry(REPOSITORY_ROOT / DEFAULT_MODELS_PATH, environ={})

    assert registry.version == 1
    assert set(registry.fallbacks) == {"factory-fast", "factory-reasoning", "factory-summary"}


def test_deployments_resolve_in_priority_order(tmp_path: Path) -> None:
    registry = load_model_registry(write(tmp_path, VALID), environ={"KEY_A": "a", "KEY_B": "b"})

    fast = [item for item in registry.deployments if item.alias == "factory-fast"]
    assert [item.priority for item in fast] == [1, 2]
    assert fast[0].model == "deepseek/deepseek-chat"


def test_a_deployment_without_its_key_is_dropped(tmp_path: Path) -> None:
    registry = load_model_registry(write(tmp_path, VALID), environ={"KEY_A": "a"})

    models = {item.model for item in registry.deployments}
    assert "openai/gpt-4o-mini" not in models
    assert "deepseek/deepseek-chat" in models


def test_a_blank_key_counts_as_missing(tmp_path: Path) -> None:
    registry = load_model_registry(write(tmp_path, VALID), environ={"KEY_A": "   "})

    assert registry.deployments == ()
    assert registry.is_usable() is False
    assert set(registry.skipped_aliases) == {"factory-fast", "factory-reasoning"}


def test_an_unconfigured_environment_is_unusable_rather_than_broken(tmp_path: Path) -> None:
    registry = load_model_registry(write(tmp_path, VALID), environ={})

    assert registry.is_usable() is False
    assert registry.aliases() == frozenset()


def test_keys_come_only_from_the_environment(tmp_path: Path) -> None:
    registry = load_model_registry(write(tmp_path, VALID), environ={"KEY_A": CANARY_KEY})

    assert CANARY_KEY not in write(tmp_path, VALID).read_text(encoding="utf-8")
    assert all(item.api_key == CANARY_KEY for item in registry.deployments)


def test_a_literal_key_in_the_document_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace("api_key_env: KEY_A", f"api_key: {CANARY_KEY}")

    with pytest.raises(MesError):
        load_model_registry(write(tmp_path, body), environ={})


def test_a_duplicate_alias_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace("alias: factory-reasoning", "alias: factory-fast")

    with pytest.raises(MesError, match="twice"):
        load_model_registry(write(tmp_path, body), environ={})


def test_a_fallback_to_an_unknown_alias_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace("fallbacks: [factory-reasoning]", "fallbacks: [factory-ghost]")

    with pytest.raises(MesError, match="unknown alias"):
        load_model_registry(write(tmp_path, body), environ={})


def test_a_self_referencing_fallback_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace("fallbacks: [factory-reasoning]", "fallbacks: [factory-fast]")

    with pytest.raises(MesError, match="itself"):
        load_model_registry(write(tmp_path, body), environ={})


def test_an_alias_without_deployments_is_rejected(tmp_path: Path) -> None:
    body = """
version: 1
aliases:
  - alias: factory-fast
    deployments: []
"""

    with pytest.raises(MesError, match="validation"):
        load_model_registry(write(tmp_path, body), environ={})


def test_an_unknown_field_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace("priority: 1", "priority: 1\n        weight: 5")

    with pytest.raises(MesError, match="validation"):
        load_model_registry(write(tmp_path, body), environ={})


def test_a_missing_registry_is_reported_as_a_structured_error(tmp_path: Path) -> None:
    with pytest.raises(MesError, match="not found"):
        load_model_registry(tmp_path / "absent.yaml", environ={})


def test_malformed_yaml_is_reported_as_a_structured_error(tmp_path: Path) -> None:
    with pytest.raises(MesError, match="valid YAML"):
        load_model_registry(write(tmp_path, "version: 1\naliases: [oops"), environ={})
