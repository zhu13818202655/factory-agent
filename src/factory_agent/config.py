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
    # Export artifact store (即时生成、直接下载、落盘保留). Generated XLSX is
    # written to a local directory (类网盘的本地实现；生产可替换为对象存储) and
    # stays downloadable across restarts until the retention window closes.
    # Expired artifacts are purged lazily; the in-memory cache only bounds RAM.
    export_store_dir: Path = Path("data/exports")
    export_retention_seconds: int = Field(default=604800, ge=60)
    export_buffer_max_entries: int = Field(default=512, ge=1)
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    request_id_header: str = "X-Request-ID"

    # MES credential contract (docs/product/AI问答对外接口-整理.md §2). The
    # caller presents an encrypted app_key in this header; the agent exchanges
    # it at /api/system/token. Identity never arrives via any other header.
    credential_header: str = "X-Factory-Credential"
    #: Proactive accessToken refresh threshold (seconds before expiry).
    mes_token_refresh_threshold_seconds: int = Field(default=300, ge=60)

    # MES resource pagination (BoundedPager). The first page probes with
    # ``mes_page_size``; when total would need more than 5 pages the pager
    # resizes once toward ``mes_page_size_max`` and re-walks, so a month of
    # barcode rows (~3e4) completes in a handful of pages. The real upper
    # bound the customer interface honors is unverified: if it caps ``size``
    # below the escalated value, pages come back short and the run surfaces
    # as incomplete instead of silently truncating.
    mes_page_size: int = Field(default=2000, ge=1)
    mes_page_size_max: int = Field(default=50000, ge=1)
    mes_max_pages: int = Field(default=20, ge=1)
    mes_max_rows: int = Field(default=250000, ge=1)

    # Time-range policy. The customer confirms queries span at most the past
    # year; wider requests terminate with a friendly notice before any MES call.
    time_range_max_days: int = Field(default=366, ge=1)

    # Delivery-warning defaults (docs/product/需求及方案整理.md 老板功能表).
    # Threshold = max(1, ceil(total_duration * ratio%)); a missing order start
    # date falls back to a fixed window.
    delivery_warning_ratio_percent: int = Field(default=10, ge=1, le=100)
    delivery_warning_fallback_days: int = Field(default=7, ge=1)

    # Role-consistency validation stage label. The customer MES pre-filters
    # rows by role (所见即所得), so findings are advisory-only: recorded for
    # review, alerted, and logged as warnings — never a block on data return.
    validation_mode: Literal["strict", "production"] = "production"

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

    # Out-of-band endpoint health probing (ADR-0006). litellm already falls
    # over reactively, but only after a request has paid 1 + llm_num_retries
    # failed attempts; probing moves that cost off the user's wait. Probes run
    # once at startup and then every llm_health_probe_interval_seconds
    # (0 = startup only). A startup probe failure never blocks startup: the
    # reviewed registry stays in place and litellm's own fallback still covers
    # the request path.
    llm_health_probe_interval_seconds: int = Field(default=300, ge=0)
    llm_health_probe_timeout_seconds: float = Field(default=3.0, gt=0.0)
    llm_health_probe_failures_to_demote: int = Field(default=2, ge=1)

    # Thinking-mode policy, global across all aliases (ADR-0006 boundary).
    # Default: thinking OFF (both current endpoints default to thinking ON, so
    # the gateway sends an explicit disable). When enabled the effort defaults
    # to "high"; accepted values low/medium/high/max are mapped per provider
    # (Qwen chat_template_kwargs.thinking_effort only knows low/medium/high,
    # DeepSeek reasoning_effort maps medium/xhigh to high).
    llm_thinking_enabled: bool = False
    llm_thinking_effort: Literal["low", "medium", "high", "max"] = "high"

    # Session orchestration bounds.
    factory_timezone: str = "Asia/Shanghai"
    session_max_input_chars: int = Field(default=2000, gt=0)
    session_max_clarification_rounds: int = Field(default=3, ge=1)
    session_history_max_turns: int = Field(default=8, ge=1)
    session_history_max_chars: int = Field(default=8192, gt=0)
    session_heartbeat_seconds: float = Field(default=15.0, gt=0.0)
    #: Follow budget for an SSE connection tailing a run claimed by another
    #: connection; on exhaustion the stream ends with an explicit wire-only
    #: terminal event instead of closing silently.
    session_follow_timeout_seconds: float = Field(default=600.0, gt=0.0)
    #: A ``running`` interaction with no persisted update for this long is
    #: treated as an orphaned run (its executor connection died) and is marked
    #: failed by the next connecting/following stream.
    session_stale_running_seconds: float = Field(default=600.0, gt=0.0)
    #: Whole-run wall-clock budget for the background interaction executor
    #: when exceeded at a cooperative stop point the run is failed
    #: durably with ``run_timeout``. Must stay below
    #: ``session_stale_running_seconds`` so a live-but-slow run is failed by
    #: its own budget before any follower can mistake it for an orphan.
    session_run_timeout_seconds: float = Field(default=300.0, gt=0.0)
    #: A ``pending`` interaction older than this never had a stream claim it:
    #: the question is persisted but no connection ever subscribed, so the run
    #: can never start and no stream-driven recovery would ever terminate it.
    #: The recovery sweeps fail such rows durably with ``abandoned``.
    session_abandoned_pending_seconds: float = Field(default=600.0, gt=0.0)
    #: Interval of the periodic recovery sweep (abandoned ``pending`` rows and
    #: orphaned ``running`` runs with no connection tailing them). Zero disables
    #: the periodic sweep; the startup sweep always runs.
    session_sweep_interval_seconds: float = Field(default=300.0, ge=0.0)


@lru_cache
def get_settings() -> FactoryAgentSettings:
    return FactoryAgentSettings()
