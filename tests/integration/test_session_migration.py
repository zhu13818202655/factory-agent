"""The Alembic baseline must build exactly the schema the store queries.

Schema drift between ``migrations/versions`` and ``persistence/tables.py``
passes every offline test and then fails in production, so this suite upgrades
a real database and reflects the result back.

Set ``FACTORY_AGENT_TEST_POSTGRES_URL`` to a disposable database to enable it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from factory_agent.persistence.engine import create_migration_engine, normalize_dsn
from factory_agent.persistence.tables import METADATA

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("FACTORY_AGENT_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set FACTORY_AGENT_TEST_POSTGRES_URL to a disposable database to run these tests",
)


def alembic_config() -> Config:
    assert DATABASE_URL is not None
    config = Config()
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", normalize_dsn(DATABASE_URL))
    return config


@pytest.fixture
def clean_database() -> Iterator[sa.Engine]:
    assert DATABASE_URL is not None
    engine = create_migration_engine(DATABASE_URL)

    def drop_everything() -> None:
        with engine.begin() as connection:
            METADATA.drop_all(connection)
            connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))

    drop_everything()
    try:
        yield engine
    finally:
        drop_everything()
        engine.dispose()


def reflected(engine: sa.Engine) -> sa.MetaData:
    metadata = sa.MetaData()
    with engine.connect() as connection:
        metadata.reflect(connection)
    return metadata


def test_upgrade_creates_every_table_the_store_queries(clean_database: sa.Engine) -> None:
    command.upgrade(alembic_config(), "head")

    tables = reflected(clean_database).tables

    for expected in METADATA.tables.values():
        assert expected.name in tables


def test_upgrade_creates_every_column_the_store_queries(clean_database: sa.Engine) -> None:
    command.upgrade(alembic_config(), "head")

    tables = reflected(clean_database).tables

    for expected in METADATA.tables.values():
        assert {column.name for column in expected.columns} == {
            column.name for column in tables[expected.name].columns
        }, expected.name


def test_upgrade_preserves_the_cascade_that_session_deletion_relies_on(
    clean_database: sa.Engine,
) -> None:
    command.upgrade(alembic_config(), "head")

    with clean_database.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT tc.table_name, rc.delete_rule"
                " FROM information_schema.table_constraints tc"
                " JOIN information_schema.referential_constraints rc"
                " ON tc.constraint_name = rc.constraint_name"
                " WHERE tc.constraint_type = 'FOREIGN KEY'"
            )
        ).all()
    rules = {str(row[0]): str(row[1]) for row in rows}

    assert rules["agent_message"] == "CASCADE"
    assert rules["agent_interaction_event"] == "CASCADE"


def test_downgrade_removes_every_table_it_created(clean_database: sa.Engine) -> None:
    config = alembic_config()
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    remaining = set(reflected(clean_database).tables)
    assert remaining & set(METADATA.tables) == set()


def test_upgrade_is_repeatable_after_a_downgrade(clean_database: sa.Engine) -> None:
    config = alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    command.upgrade(config, "head")

    assert "agent_interaction" in reflected(clean_database).tables
