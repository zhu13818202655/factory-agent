"""Shared fixtures for repository tests (Story 10).

The mock-mes app is PG-backed; tests that need it require
``MOCK_MES_TEST_DATABASE_URL`` and skip when it is not set. The schema and the
fixed test window are applied once per session.
"""

from __future__ import annotations

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
