"""Ownership-filtered SQL statements for session persistence.

These builders are the single source of every durable session query. They are
pure so a unit test can compile them and prove that no statement can run without
the trusted ``(tenant_id, user_id)`` predicate.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.sql import Select

from factory_agent.persistence.tables import (
    event_table,
    interaction_table,
    message_table,
)


class CursorError(ValueError):
    """Raised when a pagination cursor is missing, malformed, or truncated."""


def encode_cursor(created_at: datetime, row_id: str) -> str:
    payload = json.dumps({"at": created_at.isoformat(), "id": row_id}, sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        decoded: object = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, binascii.Error) as exc:
        raise CursorError("pagination cursor is malformed") from exc
    if not isinstance(decoded, dict):
        raise CursorError("pagination cursor is malformed")
    payload = cast("dict[str, object]", decoded)
    raw_at: object = payload.get("at")
    raw_id: object = payload.get("id")
    if not isinstance(raw_at, str) or not isinstance(raw_id, str):
        raise CursorError("pagination cursor is malformed")
    try:
        return datetime.fromisoformat(raw_at), raw_id
    except ValueError as exc:
        raise CursorError("pagination cursor is malformed") from exc


def _owned(table: sa.Table, tenant_id: str, user_id: str) -> sa.ColumnElement[bool]:
    return sa.and_(table.c.tenant_id == tenant_id, table.c.user_id == user_id)


def select_interaction(tenant_id: str, user_id: str, interaction_id: str) -> Select[Any]:
    return sa.select(interaction_table).where(
        _owned(interaction_table, tenant_id, user_id),
        interaction_table.c.interaction_id == interaction_id,
    )


def select_events(
    tenant_id: str, user_id: str, interaction_id: str, after_sequence: int
) -> Select[Any]:
    return (
        sa.select(event_table)
        .where(
            _owned(event_table, tenant_id, user_id),
            event_table.c.interaction_id == interaction_id,
            event_table.c.sequence > after_sequence,
        )
        .order_by(event_table.c.sequence.asc())
    )


def select_messages(
    tenant_id: str,
    user_id: str,
    session_id: str,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
) -> Select[Any]:
    statement = sa.select(message_table).where(
        _owned(message_table, tenant_id, user_id),
        message_table.c.session_id == session_id,
    )
    if cursor is not None:
        created_at, message_id = cursor
        statement = statement.where(
            sa.tuple_(message_table.c.created_at, message_table.c.message_id)
            > sa.tuple_(sa.literal(created_at), sa.literal(message_id))
        )
    return statement.order_by(
        message_table.c.created_at.asc(), message_table.c.message_id.asc()
    ).limit(limit + 1)


def select_interactions(
    tenant_id: str,
    user_id: str,
    session_id: str,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
) -> Select[Any]:
    statement = sa.select(interaction_table).where(
        _owned(interaction_table, tenant_id, user_id),
        interaction_table.c.session_id == session_id,
    )
    if cursor is not None:
        created_at, interaction_id = cursor
        statement = statement.where(
            sa.tuple_(interaction_table.c.created_at, interaction_table.c.interaction_id)
            > sa.tuple_(sa.literal(created_at), sa.literal(interaction_id))
        )
    return statement.order_by(
        interaction_table.c.created_at.asc(), interaction_table.c.interaction_id.asc()
    ).limit(limit + 1)


def claim_interaction_run(
    tenant_id: str, user_id: str, interaction_id: str, now: datetime
) -> sa.Update:
    """Compare-and-set that only one concurrent connection can win."""
    return (
        sa.update(interaction_table)
        .where(
            _owned(interaction_table, tenant_id, user_id),
            interaction_table.c.interaction_id == interaction_id,
            interaction_table.c.status == "pending",
        )
        .values(status="running", updated_at=now)
        .returning(*interaction_table.c)
    )


def delete_session(tenant_id: str, user_id: str, session_id: str) -> sa.Delete:
    return sa.delete(interaction_table).where(
        _owned(interaction_table, tenant_id, user_id),
        interaction_table.c.session_id == session_id,
    )


#: Statement builders that must always carry the ownership predicate.
OWNERSHIP_SCOPED_BUILDERS: tuple[str, ...] = (
    "select_interaction",
    "select_events",
    "select_messages",
    "select_interactions",
    "claim_interaction_run",
    "delete_session",
)


__all__ = [
    "OWNERSHIP_SCOPED_BUILDERS",
    "CursorError",
    "claim_interaction_run",
    "decode_cursor",
    "delete_session",
    "encode_cursor",
    "select_events",
    "select_interaction",
    "select_interactions",
    "select_messages",
]
