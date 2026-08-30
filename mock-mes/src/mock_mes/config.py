from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository-root ``.env`` (git-ignored) so host-side commands and tests pick
#: up the local database URLs without exporting them. Docker Compose reads the
#: same file for its own variable substitution, so the compose mock-mes
#: service pins its container-internal DSN explicitly (see compose.yaml).
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class MockMesSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_MES_", extra="ignore", env_file=_ENV_FILE)

    environment: Literal["development", "test"] = "development"
    host: str = "127.0.0.1"
    port: int = 8010
    seed: int = 20260821
    virtual_now: datetime = datetime.fromisoformat("2026-08-21T08:00:00+00:00")
    #: PostgreSQL data base (Story 10). The API is read-only against it; the
    #: generator process writes. Credentials come only from the environment.
    database_url: SecretStr | None = None
    #: Data-window start; defaults to January 1st of the previous year.
    data_start: date | None = None
    #: Optional override of the data-window end (never later than virtual_now).
    data_end: date | None = None

    # --- Factory scale (real-world magnitude; every value has a default) ------
    #: Total headcount of the primary company (COMPANY-A).
    headcount: int = 500
    #: Department (workshop) count of the primary company.
    departments: int = 5
    #: Workers per group; one group leader (role 01) per group.
    group_size: int = 10
    #: Headcount of the secondary company (COMPANY-B), used for tenant isolation.
    headcount_secondary: int = 50
    #: Share of workers that record output on a given workday.
    daily_active_ratio: float = 0.8
    #: Scan records produced per active worker per workday.
    scans_per_worker: int = 2
    #: New hires per day (0 keeps the headcount fixed; >0 grows master data).
    daily_hires: int = 0
    #: Production plans created per workday. Kept moderate: every order also
    #: drives per-order progress lookups, so very large values are unrealistic
    #: for a plant this size and slow the downstream recipes down.
    plans_per_day: int = 3
    #: Style (款号) count in the catalogue; the first two are the anchored ones.
    styles: int = 24

    @property
    def company_b_departments(self) -> int:
        """The secondary company is a scaled-down copy (tenant isolation only)."""
        return max(self.departments // 5, 1)

    @property
    def resolved_data_start(self) -> date:
        if self.data_start is not None:
            return self.data_start
        today = date.today()
        return date(today.year - 1, 1, 1)

    @property
    def resolved_data_end(self) -> date:
        if self.data_end is not None:
            end = self.data_end
        else:
            end = self.virtual_now.date()
        # Never generate data in the future (Story 10 invariant).
        return min(end, self.virtual_now.date(), date.today())


@lru_cache
def get_settings() -> MockMesSettings:
    return MockMesSettings()
