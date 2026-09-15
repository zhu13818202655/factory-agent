

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
    #: Export artifact backend. ``s3_endpoint_url`` non-empty selects the
    #: S3-compatible object store; otherwise ``export_store_dir`` selects the
    #: local directory. Both blank keeps the in-memory store (dev/test only) —
    #: exports then disappear on restart.
    s3_endpoint_url: str = ""
    s3_bucket: str = "usage-admin-exports"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_path_style: bool = True
    export_store_dir: str = ""
    timezone_name: str = "Asia/Shanghai"
    #: Secret signing platform-principal login tokens (D15); from env in prod.
    token_signing_secret: SecretStr | None = None
    #: Front-end API token (D16): configured on the front end and accepted as a
    #: Bearer token mapped to the admin role. Supports rotation by reconfig
    #: (env: ``USAGE_ADMIN_API_TOKEN``).
    api_token: SecretStr | None = None
    #: Login token lifetime.
    token_ttl_seconds: int = Field(default=28_800, ge=300, le=7 * 86_400)


@lru_cache
def get_settings() -> UsageAdminSettings:
    return UsageAdminSettings()
