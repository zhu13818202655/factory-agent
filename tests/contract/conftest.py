"""PG-backed fixtures for the repository contract tests (Story 10)."""

from __future__ import annotations

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
def contract_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("set MOCK_MES_TEST_DATABASE_URL to run mock-mes contract tests")
    return TEST_DATABASE_URL


@pytest_asyncio.fixture(scope="session")
async def contract_generated_db(contract_database_url: str) -> str:
    await upgrade_test_db(contract_database_url)
    await reset_test_db(contract_database_url)
    await generate_test_window(contract_database_url)
    return contract_database_url


@pytest_asyncio.fixture
async def contract_app(contract_generated_db: str):
    application = make_test_app(contract_generated_db)
    await application.state.db.open()
    try:
        yield application
    finally:
        await application.state.db.close()
