from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class MockMesSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_MES_", extra="ignore")

    environment: Literal["development", "test"] = "development"
    host: str = "127.0.0.1"
    port: int = 8010
    scenario: Literal["small", "standard"] = "small"
    seed: int = 20260821
    virtual_now: datetime = datetime.fromisoformat("2026-08-21T08:00:00+00:00")


@lru_cache
def get_settings() -> MockMesSettings:
    return MockMesSettings()
