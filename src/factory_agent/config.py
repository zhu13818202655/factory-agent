from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class FactoryAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FACTORY_AGENT_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    canonical_mes_base_url: AnyHttpUrl | None = None
    postgres_url: PostgresDsn | None = None
    redis_url: RedisDsn | None = None
    # Instant-export transient buffer (Story 3: 即时生成、直接下载、服务端不留存).
    # Generated XLSX lives only in a bounded in-process buffer for this short
    # window; there is no object store and no retention lifecycle.
    export_buffer_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    export_buffer_max_entries: int = Field(default=512, ge=1)
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    request_id_header: str = "X-Request-ID"

    # MES credential contract (docs/product/AI问答对外接口-整理.md §2). The
    # caller presents an encrypted app_key in this header; the agent exchanges
    # it at /api/system/token. Identity never arrives via any other header.
    credential_header: str = "X-Factory-Credential"
    #: Proactive accessToken refresh threshold inside the 2h validity window.
    mes_token_refresh_threshold_seconds: int = Field(default=5400, ge=60)
    #: The customer ``timestamp`` validity window (default 60 s); a stale
    #: bundle is re-exchanged before the next business call.
    mes_timestamp_ttl_seconds: int = Field(default=60, ge=5)

    # Time-range policy. The customer confirms queries span at most the past
    # year; wider requests terminate with a friendly notice before any MES call.
    time_range_max_days: int = Field(default=366, ge=1)

    # Delivery-warning defaults (docs/product/需求及方案整理.md 老板功能表).
    # Threshold = max(1, ceil(total_duration * ratio%)); a missing order start
    # date falls back to a fixed window. Reviewed again in Story 3 dry-runs.
    delivery_warning_ratio_percent: int = Field(default=10, ge=1, le=100)
    delivery_warning_fallback_days: int = Field(default=7, ge=1)

    # Role-consistency validation staged mode (Story 2): strict = 对接期
    # (any inconsistency blocks the result and is exposed as an integration
    # problem); production = 主路径信任 MES + two-tier handling (exact blocks
    # with alert, heuristic only logs).
    validation_mode: Literal["strict", "production"] = "strict"

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
