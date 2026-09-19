"""Configuration for the embedded statistics surface.

Every key is prefixed ``FACTORY_AGENT_STATISTICS_`` so the statistics surface
never collides with the business settings, and its two export backends never
share a bucket or directory with the artifact exporter. The database DSN is not
declared here: the statistics store opens its own connections to
``FACTORY_AGENT_POSTGRES_URL``, which keeps one deployment on one credential.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class StatisticsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FACTORY_AGENT_STATISTICS_", extra="ignore")

    #: Shared secret for signing short-lived export download links.
    export_signing_secret: SecretStr | None = None
    #: Base URL clients use to reach the statistics download endpoint.
    download_base_url: str = "http://127.0.0.1:8000"
    export_presign_expires_seconds: int = Field(default=900, ge=60, le=3600)
    #: Statistics report backend, separate from the artifact exporter: S3 wins
    #: when an endpoint is configured, then the local directory, and the
    #: in-memory store is the dev/test fallback (exports vanish on restart).
    s3_endpoint_url: str = ""
    s3_bucket: str = "factory-agent-statistics-exports"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_path_style: bool = True
    export_store_dir: str = ""
    timezone_name: str = "Asia/Shanghai"
    #: Secret signing platform-principal login tokens (D15); from env in prod.
    token_signing_secret: SecretStr | None = None
    #: Machine and bootstrap channel (D16): accepted as a Bearer token mapped to
    #: the admin role. It is also the only way to create the first operator
    #: account after a database reset, so it must never be removed.
    api_token: SecretStr | None = None
    token_ttl_seconds: int = Field(default=28_800, ge=300, le=7 * 86_400)

    #: Unknown-AppKey policy (D-3). ``auto`` admits and registers a first-seen
    #: AppKey after the customer MES accepted its credential; ``allowlist`` is
    #: reserved for deployments that require manual onboarding and is not
    #: implemented yet.
    tenant_registration_mode: Literal["auto", "allowlist"] = "auto"

    #: ``statement_timeout`` for every statistics statement. A platform-wide
    #: aggregate must never hold a connection long enough to matter.
    statement_timeout_ms: int = Field(default=30_000, ge=1000, le=300_000)


@lru_cache
def get_settings() -> StatisticsSettings:
    return StatisticsSettings()


__all__ = ["StatisticsSettings", "get_settings"]
