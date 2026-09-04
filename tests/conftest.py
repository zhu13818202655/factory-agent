"""Shared fixtures for repository tests.

The mock-mes app is PG-backed; tests that need it require
``MOCK_MES_TEST_DATABASE_URL`` and skip when it is not set. The schema and the
fixed test window are applied once per session.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
from mock_mes.testing import (
    TEST_DATABASE_URL,
    generate_test_window,
    make_test_app,
    reset_test_db,
    upgrade_test_db,
)

# ---------------------------------------------------------------------------
# litellm 在 import 时默认把仓库根 ``.env`` 注入 ``os.environ``，会把真实环境
# 配置（如 FACTORY_AGENT_CANONICAL_MES_BASE_URL=客户 MES 根地址）泄漏进单测：
# FactoryAgentSettings 因此装配 token 网关，凡走 tenant/user 降级头的 API 测试
# 一律 401。这里在收集任何测试模块之前主动触发一次注入并立即清除
# FACTORY_AGENT_*，保证测试内的 settings 构造只看到测试本意提供的环境。
# ---------------------------------------------------------------------------
import os as _os

try:
    import litellm as _litellm  # noqa: F401  (import 副作用：触发一次 .env 注入)
except Exception:  # pragma: no cover - 环境缺 litellm 时无需清理
    pass
for _k in list(_os.environ):
    if _k.startswith("FACTORY_AGENT_"):
        del _os.environ[_k]


@pytest.fixture(autouse=True)
def _loguru_forward_to_logging() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Bridge Loguru records back into stdlib ``logging`` so ``caplog`` works.

    The application logs through Loguru (ADR-0004); tests assert on log
    content via ``caplog``. This fixture forwards every Loguru record to the
    stdlib logger named after its ``component``, then resets Loguru handlers
    so a previous test's ``configure_logging`` sink cannot leak into the next.
    """
    from loguru import logger as loguru_logger

    # A prior test may have installed the logging→Loguru bridge via
    # ``configure_logging``; remove it so forwarded records do not loop back.
    logging.root.handlers = [
        handler
        for handler in logging.root.handlers
        if type(handler).__module__ != "factory_agent.observability.logging_adapter"
    ]

    def _forward(message: Any) -> None:
        record = message.record
        name = record["extra"].get("component", "app")
        std_logger = logging.getLogger(name)
        std_logger.handle(
            logging.LogRecord(
                name=name,
                level=record["level"].no,
                pathname=record["file"].path,
                lineno=record["line"],
                msg=record["message"],
                args=(),
                exc_info=record["exception"],
                func=record["function"],
            )
        )

    loguru_logger.remove()
    loguru_logger.add(_forward, level=0)
    yield


@pytest.fixture(scope="session")
def mock_mes_test_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("set MOCK_MES_TEST_DATABASE_URL to run mock-mes-backed tests")
    return TEST_DATABASE_URL


@pytest_asyncio.fixture(scope="session")
async def mock_mes_db(mock_mes_test_url: str) -> str:
    await upgrade_test_db(mock_mes_test_url)
    await reset_test_db(mock_mes_test_url)
    await generate_test_window(mock_mes_test_url)
    return mock_mes_test_url


@pytest_asyncio.fixture
async def mock_mes_app(mock_mes_db: str) -> Any:
    application = make_test_app(mock_mes_db)
    await application.state.db.open()
    try:
        yield application
    finally:
        await application.state.db.close()
