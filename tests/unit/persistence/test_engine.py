from __future__ import annotations

import pytest

from factory_agent.persistence.engine import normalize_dsn

CANARY = "postgres_dev_password"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
    ],
)
def test_the_installed_driver_is_pinned(url: str, expected: str) -> None:
    assert normalize_dsn(url) == expected


def test_an_explicit_driver_is_left_alone() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db"

    assert normalize_dsn(url) == url


@pytest.mark.parametrize("url", ["", "not-a-url", "mysql://u:p@h/db", "sqlite:///db.sqlite3"])
def test_non_postgresql_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_dsn(url)


def test_normalization_preserves_credentials_without_logging_them() -> None:
    normalized = normalize_dsn(f"postgresql://app:{CANARY}@db:5432/app")

    assert normalized.endswith(f"app:{CANARY}@db:5432/app")


def test_psycopg2_is_never_selected() -> None:
    assert "psycopg2" not in normalize_dsn("postgresql://u:p@h/db")
