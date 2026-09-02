"""Fixtures for integration tests that need the real mock-mes database."""

from __future__ import annotations

import pytest
from mock_mes.testing import TEST_DATABASE_URL


@pytest.fixture(scope="session")
def mock_mes_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("set MOCK_MES_TEST_DATABASE_URL to run mock-mes-backed integration tests")
    return TEST_DATABASE_URL
