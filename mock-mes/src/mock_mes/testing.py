"""Test helpers for the PG-backed mock-mes.

Shared by ``mock-mes/tests`` and the repository contract tests. All helpers
require ``MOCK_MES_TEST_DATABASE_URL`` pointing at a disposable database; tests
that need PG skip when it is not set (same convention as usage-admin).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
import psycopg.rows
from dotenv import load_dotenv
from pydantic import SecretStr

from mock_mes.config import MockMesSettings
from mock_mes.generator.engine import fill_window

# Load the repository-root ``.env`` (git-ignored) so
# ``MOCK_MES_TEST_DATABASE_URL`` works without exporting it; real environment
# variables take precedence over the file.
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / ".env")

TEST_DATABASE_URL = os.environ.get("MOCK_MES_TEST_DATABASE_URL")

#: Fixed test window covering every anchor date.
TEST_WINDOW_START = date(2026, 7, 1)
TEST_WINDOW_END = date(2026, 8, 21)
TEST_SEED = 20260821
TEST_VIRTUAL_NOW = "2026-08-21T08:00:00+00:00"


def require_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        raise RuntimeError("set MOCK_MES_TEST_DATABASE_URL to a disposable database")
    return TEST_DATABASE_URL


async def upgrade_test_db(url: str) -> None:
    """Apply the mock-mes Alembic migration head to the test database."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config(os.path.join(project_root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(project_root, "migrations"))
    config.set_main_option(
        "sqlalchemy.url", url.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    await asyncio.to_thread(command.upgrade, config, "head")


async def connect_db(url: str) -> psycopg.AsyncConnection[Any]:
    """Open a dict-row async connection (helper hides psycopg typing quirks)."""
    return await psycopg.AsyncConnection.connect(
        url,
        row_factory=psycopg.rows.dict_row,  # type: ignore[arg-type]
    )


_ALL_TABLES = (
    "mock_dept",
    "mock_employee",
    "mock_huohao",
    "mock_sc_type",
    "mock_rfid_worktype",
    "mock_huohao_worktype",
    "mock_user_info",
    "mock_move_menu",
    "mock_dg",
    "mock_dg_zu",
    "mock_plan",
    "mock_sclzd",
    "mock_sclzd_worktype",
    "mock_barcode",
    "mock_barcode_cl",
    "mock_dg_cl",
    "mock_pin_feng",
    "mock_ysk",
    "mock_wsk",
    "mock_generate_batch",
)


async def reset_test_db(url: str) -> None:
    """Empty every mock_* table so a test window is self-contained."""
    async with await connect_db(url) as connection:
        await connection.execute("TRUNCATE " + ", ".join(_ALL_TABLES))


async def generate_test_window(url: str) -> None:
    """Generate the fixed test window (idempotent; skips already-done days)."""
    async with await connect_db(url) as connection:
        await fill_window(
            connection, _test_settings(), TEST_WINDOW_START, TEST_WINDOW_END, "test-window"
        )


def _test_settings() -> MockMesSettings:
    """Scale settings for the test data base (same defaults as production)."""
    return MockMesSettings(seed=TEST_SEED, virtual_now=datetime.fromisoformat(TEST_VIRTUAL_NOW))


def make_test_app(url: str):
    """Build the app against the test database (pool not yet open)."""
    from datetime import datetime

    from mock_mes.api.server import create_app

    settings = MockMesSettings(
        environment="test",
        database_url=SecretStr(url),
        seed=TEST_SEED,
        virtual_now=datetime.fromisoformat(TEST_VIRTUAL_NOW),
        data_start=TEST_WINDOW_START,
        data_end=TEST_WINDOW_END,
    )
    return create_app(settings)


__all__ = [
    "TEST_DATABASE_URL",
    "TEST_SEED",
    "TEST_VIRTUAL_NOW",
    "TEST_WINDOW_END",
    "TEST_WINDOW_START",
    "generate_test_window",
    "make_test_app",
    "require_test_database_url",
    "reset_test_db",
    "upgrade_test_db",
]
