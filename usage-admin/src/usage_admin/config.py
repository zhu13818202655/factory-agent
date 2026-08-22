from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class UsageAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="USAGE_ADMIN_", extra="ignore")

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8020
    database_url: SecretStr | None = None


@lru_cache
def get_settings() -> UsageAdminSettings:
    return UsageAdminSettings()
