from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from factory_agent.persistence import queries
from factory_agent.persistence.tables import METADATA, message_table

DIALECT = postgresql.dialect()
NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"
USER = "user-a"

# ``ClauseElement`` rather than ``Executable``: only the former exposes ``compile``.
Statement = sa.sql.ClauseElement


def compiled(statement: Statement) -> str:
    return str(statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True}))


OWNERSHIP_STATEMENTS: dict[str, Statement] = {
    "select_interaction": queries.select_interaction(TENANT, USER, "i-1"),
    "select_events": queries.select_events(TENANT, USER, "i-1", 0),
    "select_messages": queries.select_messages(TENANT, USER, "s-1", 50),
    "select_interactions": queries.select_interactions(TENANT, USER, "s-1", 50),
    "claim_interaction_run": queries.claim_interaction_run(TENANT, USER, "i-1", NOW),
    "delete_session": queries.delete_session(TENANT, USER, "s-1"),
}


def test_every_ownership_scoped_builder_is_covered() -> None:
    assert set(queries.OWNERSHIP_SCOPED_BUILDERS) == set(OWNERSHIP_STATEMENTS)


@pytest.mark.parametrize("name", sorted(OWNERSHIP_STATEMENTS))
def test_business_statements_always_filter_by_trusted_tenant_and_user(name: str) -> None:
    sql = compiled(OWNERSHIP_STATEMENTS[name])

    assert f"tenant_id = '{TENANT}'" in sql
    assert f"user_id = '{USER}'" in sql


@pytest.mark.parametrize("name", sorted(OWNERSHIP_STATEMENTS))
def test_ownership_predicates_are_conjunctive_not_optional(name: str) -> None:
    sql = compiled(OWNERSHIP_STATEMENTS[name]).lower()

    assert " or " not in sql


def test_cursor_pagination_is_stable_and_over_fetches_one_row() -> None:
    sql = compiled(queries.select_messages(TENANT, USER, "s-1", 25))

    assert "ORDER BY" in sql
    assert "created_at ASC" in sql
    assert "message_id ASC" in sql
    assert "LIMIT 26" in sql


def test_cursor_round_trips() -> None:
    cursor = queries.encode_cursor(NOW, "m-1")

    assert queries.decode_cursor(cursor) == (NOW, "m-1")


@pytest.mark.parametrize("cursor", ["", "not-base64!!", "e30=", "eyJhdCI6IDF9"])
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(queries.CursorError):
        queries.decode_cursor(cursor)


def test_cursor_narrows_the_result_window() -> None:
    sql = compiled(queries.select_messages(TENANT, USER, "s-1", 25, (NOW, "m-1")))

    assert "'m-1'" in sql
    assert f"tenant_id = '{TENANT}'" in sql


def test_messages_and_events_cascade_from_the_interaction() -> None:
    cascading = {
        table.name: {constraint.ondelete for constraint in table.foreign_key_constraints}
        for table in METADATA.tables.values()
        if table.foreign_key_constraints
    }

    assert cascading["agent_message"] == {"CASCADE"}
    assert cascading["agent_interaction_event"] == {"CASCADE"}


def test_message_sequence_is_unique_within_an_interaction() -> None:
    unique = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in message_table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert ("interaction_id", "sequence") in unique
