"""Outbox publisher process wiring tests.

The publisher is a separate process: its configuration requirements and script
registration are what Story 8 wires. The retry/dead-letter/backlog semantics
themselves are covered in ``test_publisher.py``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from factory_agent.config import get_settings
from factory_agent.usage.publisher_cli import run_forever

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_publisher_requires_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_AGENT_POSTGRES_URL", raising=False)
    monkeypatch.delenv("FACTORY_AGENT_USAGE_ADMIN_BASE_URL", raising=False)
    get_settings.cache_clear()

    with pytest.raises(SystemExit, match="FACTORY_AGENT_POSTGRES_URL"):
        await run_forever()


@pytest.mark.asyncio
async def test_publisher_requires_usage_admin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_AGENT_POSTGRES_URL", "postgresql+psycopg://x:x@localhost/db")
    monkeypatch.delenv("FACTORY_AGENT_USAGE_ADMIN_BASE_URL", raising=False)
    get_settings.cache_clear()

    with pytest.raises(SystemExit, match="FACTORY_AGENT_USAGE_ADMIN_BASE_URL"):
        await run_forever()


def test_publish_script_is_registered() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    assert scripts["factory-agent-publish"] == "factory_agent.usage.publisher_cli:main"


def test_publisher_wires_batch_settings_from_config() -> None:
    from factory_agent.config import FactoryAgentSettings

    settings = FactoryAgentSettings(
        usage_outbox_batch_size=25,
        usage_outbox_poll_seconds=3.0,
        usage_outbox_max_attempts=5,
    )
    assert settings.usage_outbox_batch_size == 25
    assert settings.usage_outbox_poll_seconds == 3.0
    assert settings.usage_outbox_max_attempts == 5
