from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class UsageAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="USAGE_ADMIN_", extra="ignore")

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8020
    database_url: SecretStr | None = None
    #: Shared secret for signing short-lived export download links.
    export_signing_secret: SecretStr | None = None
    #: Base URL clients use to reach this service's download endpoint.
    download_base_url: str = "http://127.0.0.1:8020"
    export_presign_expires_seconds: int = Field(default=900, ge=60, le=3600)
    #: Optional bearer token guarding the internal ingest endpoint. When unset
    #: the endpoint accepts requests in development mode only.
    ingest_api_key: SecretStr | None = None
    timezone_name: str = "Asia/Shanghai"
    ingest_batch_max_events: int = Field(default=1000, ge=1)
    ingest_batch_max_bytes: int = Field(default=1_000_000, ge=1024)


@lru_cache
def get_settings() -> UsageAdminSettings:
    return UsageAdminSettings()
