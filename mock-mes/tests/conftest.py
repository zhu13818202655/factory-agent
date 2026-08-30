"""PG-backed fixtures for mock-mes tests (Story 10).

Requires ``MOCK_MES_TEST_DATABASE_URL``; tests that need PostgreSQL skip when
it is not set. The schema migration and the fixed test window are applied once
per session.
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
def database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("set MOCK_MES_TEST_DATABASE_URL to run PostgreSQL-backed tests")
    return TEST_DATABASE_URL


@pytest_asyncio.fixture(scope="session")
async def generated_db(database_url: str) -> str:
    await upgrade_test_db(database_url)
    await reset_test_db(database_url)
    # Fill the fixed test window (idempotent; already-generated days skip).
    await generate_test_window(database_url)
    return database_url


@pytest_asyncio.fixture
async def app(generated_db: str) -> Any:
    application = make_test_app(generated_db)
    await application.state.db.open()
    try:
        yield application
    finally:
        await application.state.db.close()


@pytest_asyncio.fixture
async def client(app: Any):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
