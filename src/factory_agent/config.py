from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class FactoryAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FACTORY_AGENT_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    canonical_mes_base_url: AnyHttpUrl | None = None
    postgres_url: PostgresDsn | None = None
    redis_url: RedisDsn | None = None
    artifact_endpoint: AnyHttpUrl | None = None
    artifact_bucket: str | None = None
    artifact_region: str = "us-east-1"
    artifact_access_key: SecretStr | None = None
    artifact_secret_key: SecretStr | None = None
    #: Presigned download links default to a short 15-minute lifetime.
    artifact_presign_expires_seconds: int = Field(default=900, ge=60, le=3600)
    #: Exported artifacts are retained for 3 months and then cleaned up (K5).
    artifact_cleanup_after_days: int = Field(default=90, ge=1)
    artifact_max_size_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    #: Object keys are unguessable UUIDs; no employee IDs or question text.
    artifact_secret_prefix: str = "factory-agent"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    request_id_header: str = "X-Request-ID"

    # LLM boundary (ADR-0006). Deployments and fallback order come from the
    # reviewed registry; provider keys come from the environment variables that
    # registry names. No provider key or URL is ever declared here.
    model_registry_path: Path = Path("configs/knowledge/models.yaml")
    llm_fast_alias: str = "factory-fast"
    llm_reasoning_alias: str = "factory-reasoning"
    llm_summary_alias: str = "factory-summary"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    llm_timeout_seconds: float = Field(default=30.0, gt=0.0)
    llm_max_output_tokens: int = Field(default=2048, gt=0)
    llm_max_repair_attempts: int = Field(default=1, ge=0, le=1)
    llm_num_retries: int = Field(default=2, ge=0, le=5)
    llm_allowed_fails: int = Field(default=2, ge=1)
    llm_cooldown_seconds: int = Field(default=30, ge=1)

    # Session orchestration bounds.
    factory_timezone: str = "Asia/Shanghai"
    session_max_input_chars: int = Field(default=2000, gt=0)
    session_max_clarification_rounds: int = Field(default=3, ge=1)
    session_history_max_turns: int = Field(default=8, ge=1)
    session_history_max_chars: int = Field(default=8192, gt=0)
    session_heartbeat_seconds: float = Field(default=15.0, gt=0.0)


@lru_cache
def get_settings() -> FactoryAgentSettings:
    return FactoryAgentSettings()
