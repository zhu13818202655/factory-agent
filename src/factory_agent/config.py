from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class FactoryAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FACTORY_AGENT_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    canonical_mes_base_url: AnyHttpUrl | None = None
    postgres_url: PostgresDsn | None = None
    litellm_base_url: AnyHttpUrl | None = None
    redis_url: RedisDsn | None = None
    artifact_endpoint: AnyHttpUrl | None = None
    artifact_bucket: str | None = None


@lru_cache
def get_settings() -> FactoryAgentSettings:
    return FactoryAgentSettings()
