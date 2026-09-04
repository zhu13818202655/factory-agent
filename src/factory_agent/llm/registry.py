"""Reviewed model deployment registry (ADR-0006).

Loaded from ``configs/knowledge/models.yaml`` and validated with a strict
schema, so an unreviewed or malformed deployment can never reach the router.

API keys never appear in the document: each deployment names an environment
variable, which is resolved here into a ``SecretStr``. Nothing in this module
returns a raw key, and ``__repr__`` of the resolved values stays redacted.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from factory_agent.domain.errors import InvalidRequestError

DEFAULT_MODELS_PATH = Path("configs/knowledge/models.yaml")


class ModelDeploymentEntry(BaseModel):
    """One upstream deployment behind a logical alias.

    ``api_key_env`` is mandatory: a key is never written into the reviewed
    document. ``api_base_env`` / ``model_env`` are optional overrides — when
    set and the named variable is non-empty, the environment wins over the
    checked-in ``api_base`` / ``model`` defaults. That keeps one reviewed
    topology in git while local/dev deployments retarget a self-hosted or
    staging endpoint purely through the environment (see ADR-0006).

    ``provider`` is the LiteLLM provider prefix (``openai``, ``deepseek``, ...)
    used to qualify the model id. Self-hosted OpenAI-compatible endpoints
    (vLLM / SGLang) serve model ids that LiteLLM cannot map to a provider on
    its own — routing ``Qwen/...`` unqualified raises "LLM Provider NOT
    provided" at Router construction. Declaring it here keeps the qualification
    reviewed rather than guessed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    api_base: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    provider: str | None = None
    api_base_env: str | None = None
    model_env: str | None = None
    priority: int = Field(default=1, ge=1)


class ModelAliasEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(min_length=1)
    deployments: tuple[ModelDeploymentEntry, ...] = Field(min_length=1)
    fallbacks: tuple[str, ...] = ()


class ModelRegistryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    aliases: tuple[ModelAliasEntry, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ResolvedDeployment:
    """A deployment whose key was present in the environment."""

    alias: str
    model: str
    api_base: str
    api_key: str
    priority: int
    provider: str | None = None

    @property
    def litellm_model(self) -> str:
        """Provider-qualified model id handed to the router.

        Self-hosted OpenAI-compatible servers expose arbitrary model ids that
        LiteLLM cannot map on its own, so an explicit ``provider`` wins. A model
        id already carrying that prefix is returned untouched, so
        ``deepseek/deepseek-chat`` is never double-qualified.
        """
        if not self.provider:
            return self.model
        prefix = f"{self.provider}/"
        return self.model if self.model.startswith(prefix) else f"{prefix}{self.model}"


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    """Immutable runtime view of the reviewed registry."""

    version: int
    deployments: tuple[ResolvedDeployment, ...]
    fallbacks: Mapping[str, tuple[str, ...]]
    skipped_aliases: tuple[str, ...] = ()

    def aliases(self) -> frozenset[str]:
        return frozenset(deployment.alias for deployment in self.deployments)

    def is_usable(self) -> bool:
        return bool(self.deployments)


def load_model_registry(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> ModelRegistry:
    """Load and validate the registry, resolving keys from the environment."""
    resolved_path = path or DEFAULT_MODELS_PATH
    try:
        raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InvalidRequestError(f"model registry not found at {resolved_path}") from exc
    except yaml.YAMLError as exc:
        raise InvalidRequestError("model registry is not valid YAML") from exc

    try:
        document = ModelRegistryDocument.model_validate(raw)
    except ValidationError as exc:
        raise InvalidRequestError(f"model registry failed validation: {exc.error_count()}") from exc

    _reject_duplicate_aliases(document)
    _reject_unknown_fallbacks(document)

    source = os.environ if environ is None else environ
    deployments: list[ResolvedDeployment] = []
    skipped: list[str] = []
    for entry in document.aliases:
        resolved = _resolve_alias(entry, source)
        if resolved:
            deployments.extend(resolved)
        else:
            skipped.append(entry.alias)

    return ModelRegistry(
        version=document.version,
        deployments=tuple(deployments),
        fallbacks={entry.alias: entry.fallbacks for entry in document.aliases},
        skipped_aliases=tuple(skipped),
    )


def _resolve_alias(
    entry: ModelAliasEntry, environ: Mapping[str, str]
) -> tuple[ResolvedDeployment, ...]:
    resolved: list[ResolvedDeployment] = []
    for deployment in sorted(entry.deployments, key=lambda item: item.priority):
        api_key = environ.get(deployment.api_key_env, "").strip()
        if not api_key:
            continue
        resolved.append(
            ResolvedDeployment(
                alias=entry.alias,
                model=_override(deployment.model_env, deployment.model, environ),
                api_base=_override(deployment.api_base_env, deployment.api_base, environ),
                api_key=api_key,
                priority=deployment.priority,
                provider=deployment.provider,
            )
        )
    return tuple(resolved)


def _override(env_name: str | None, default: str, environ: Mapping[str, str]) -> str:
    """Environment override with a non-empty-value guard; default otherwise."""
    if env_name is None:
        return default
    candidate = environ.get(env_name, "").strip()
    return candidate or default


def _reject_duplicate_aliases(document: ModelRegistryDocument) -> None:
    seen: set[str] = set()
    for entry in document.aliases:
        if entry.alias in seen:
            raise InvalidRequestError(f"model registry declares alias twice: {entry.alias}")
        seen.add(entry.alias)


def _reject_unknown_fallbacks(document: ModelRegistryDocument) -> None:
    known = {entry.alias for entry in document.aliases}
    for entry in document.aliases:
        for fallback in entry.fallbacks:
            if fallback not in known:
                raise InvalidRequestError(
                    f"alias {entry.alias} falls back to unknown alias {fallback}"
                )
            if fallback == entry.alias:
                raise InvalidRequestError(f"alias {entry.alias} falls back to itself")


__all__ = [
    "DEFAULT_MODELS_PATH",
    "ModelAliasEntry",
    "ModelDeploymentEntry",
    "ModelRegistry",
    "ModelRegistryDocument",
    "ResolvedDeployment",
    "load_model_registry",
]
