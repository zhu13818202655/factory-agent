"""Ownership-filtered SQL statements for session persistence.

These builders are the single source of every durable session query. They are
pure so a unit test can compile them and prove that no statement can run without
the trusted ``(tenant_id, user_id)`` predicate.
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.sql import Select

from factory_agent.domain import MessageKind, MessageRole
from factory_agent.persistence.tables import (
    conversation_table,
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
    exclude_kinds: tuple[str, ...] = (),
) -> Select[Any]:
    """Forward-paged messages of one owned session.

    ``exclude_kinds`` is a pure subtraction applied in SQL rather than after
    paging, so a page stays dense (a post-filter would return short pages while
    still advertising a next cursor).
    """
    statement = sa.select(message_table).where(
        _owned(message_table, tenant_id, user_id),
        message_table.c.session_id == session_id,
    )
    if exclude_kinds:
        statement = statement.where(message_table.c.kind.not_in(exclude_kinds))
    if cursor is not None:
        created_at, message_id = cursor
        statement = statement.where(
            sa.tuple_(message_table.c.created_at, message_table.c.message_id)
            > sa.tuple_(sa.literal(created_at), sa.literal(message_id))
        )
    return statement.order_by(
        message_table.c.created_at.asc(), message_table.c.message_id.asc()
    ).limit(limit + 1)


def select_latest_message(
    tenant_id: str,
    user_id: str,
    session_id: str,
    kinds: tuple[str, ...],
) -> Select[Any]:
    """Newest message of the given kinds inside one owned session.

    Descending by ``(created_at, message_id)`` with ``LIMIT 1``: the caller
    needs the most recent row, which ``select_messages`` (forward paging from
    the oldest row) cannot reach without walking the whole session.
    """
    return (
        sa.select(message_table)
        .where(
            _owned(message_table, tenant_id, user_id),
            message_table.c.session_id == session_id,
            message_table.c.kind.in_(kinds),
        )
        .order_by(message_table.c.created_at.desc(), message_table.c.message_id.desc())
        .limit(1)
    )


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


def select_conversations(
    tenant_id: str,
    user_id: str,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
) -> Select[Any]:
    """One recency-ordered page of an owner's conversations.

    Ordering is ``updated_at DESC, session_id DESC`` — the newest activity
    first, which is what the history panel shows — and the cursor predicate is
    therefore strictly *before* the last seen key.
    """
    statement = sa.select(conversation_table).where(
        _owned(conversation_table, tenant_id, user_id)
    )
    if cursor is not None:
        updated_at, session_id = cursor
        statement = statement.where(
            sa.tuple_(conversation_table.c.updated_at, conversation_table.c.session_id)
            < sa.tuple_(sa.literal(updated_at), sa.literal(session_id))
        )
    return statement.order_by(
        conversation_table.c.updated_at.desc(), conversation_table.c.session_id.desc()
    ).limit(limit + 1)


def select_conversation(tenant_id: str, user_id: str, session_id: str) -> Select[Any]:
    return sa.select(conversation_table).where(
        _owned(conversation_table, tenant_id, user_id),
        conversation_table.c.session_id == session_id,
    )


def count_conversations(tenant_id: str, user_id: str) -> Select[Any]:
    """An owner's conversation count, used to enforce the per-user cap."""
    return sa.select(sa.func.count()).select_from(conversation_table).where(
        _owned(conversation_table, tenant_id, user_id)
    )


def insert_conversation(
    tenant_id: str, user_id: str, session_id: str, *, created_at: datetime
) -> sa.Insert:
    """Create a conversation unless it already exists (idempotent).

    ``ON CONFLICT DO NOTHING`` on the ownership-complete primary key is what
    makes a repeated create-call safe under concurrency: two racing callers
    cannot produce two rows, and neither gets an error.

    ``RETURNING`` is how the caller learns whether it won: a cursor's
    ``rowcount`` is not dependable for a conflicting insert (drivers report it
    as zero even when the row was written), whereas an empty result set means
    unambiguously "someone else already had it".

    The row is written with the caller's *trusted* ownership pair — it carries no
    ownership predicate because an insert has no rows to filter, which is why it
    is deliberately not listed in ``OWNERSHIP_SCOPED_BUILDERS``; a dedicated test
    proves the values come from the trusted pair instead.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return (
        pg_insert(conversation_table)
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            created_at=created_at,
            updated_at=created_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                conversation_table.c.tenant_id,
                conversation_table.c.user_id,
                conversation_table.c.session_id,
            ]
        )
        .returning(conversation_table.c.session_id)
    )


def touch_conversation(
    tenant_id: str, user_id: str, session_id: str, *, now: datetime
) -> sa.Update:
    """Move a conversation's recency forward, never backward.

    ``GREATEST`` keeps the ordering monotonic: a late commit belonging to an
    older interaction must not demote a conversation another turn already moved
    to the top. Missing rows are left alone (the caller inserts instead).
    """
    return (
        sa.update(conversation_table)
        .where(
            _owned(conversation_table, tenant_id, user_id),
            conversation_table.c.session_id == session_id,
        )
        .values(
            updated_at=sa.case(
                (
                    conversation_table.c.updated_at > now,
                    conversation_table.c.updated_at,
                ),
                else_=now,
            )
        )
    )


def select_conversation_messages(
    tenant_id: str, user_id: str, session_ids: tuple[str, ...]
) -> Select[Any]:
    """Per session: the newest readable message *and* the readable count.

    ``DISTINCT ON (session_id)`` with a descending order yields the newest row
    of each session; the window count is evaluated over the same phase-filtered
    partition, so one statement hydrates the whole page's previews and counts.
    ``phase`` rows are excluded from both: they are stage-progress lines with no
    value in a history preview, and the frontend already drops them when
    rendering.
    """
    return (
        sa.select(
            message_table,
            sa.func.count().over(partition_by=message_table.c.session_id).label("message_count"),
        )
        .distinct(message_table.c.session_id)
        .where(
            _owned(message_table, tenant_id, user_id),
            message_table.c.session_id.in_(session_ids),
            message_table.c.kind != MessageKind.PHASE.value,
        )
        .order_by(
            message_table.c.session_id,
            message_table.c.created_at.desc(),
            message_table.c.message_id.desc(),
        )
    )


def select_conversation_first_questions(
    tenant_id: str, user_id: str, session_ids: tuple[str, ...]
) -> Select[Any]:
    """Per session: the text of its earliest user question (the title source).

    The earliest row needs the opposite ordering from
    :func:`select_conversation_messages`, so it cannot share that statement.
    """
    return (
        sa.select(message_table.c.session_id, message_table.c.text)
        .distinct(message_table.c.session_id)
        .where(
            _owned(message_table, tenant_id, user_id),
            message_table.c.session_id.in_(session_ids),
            message_table.c.role == MessageRole.USER.value,
            message_table.c.kind == MessageKind.PLAIN_TEXT.value,
        )
        .order_by(
            message_table.c.session_id,
            message_table.c.created_at.asc(),
            message_table.c.message_id.asc(),
        )
    )


def select_conversation_interactions(
    tenant_id: str, user_id: str, session_ids: tuple[str, ...]
) -> Select[Any]:
    """Per session: the newest interaction *and* the interaction count.

    Same one-statement shape as :func:`select_conversation_messages`; the newest
    row supplies ``last_status`` for the list preview.
    """
    return (
        sa.select(
            interaction_table,
            sa.func.count()
            .over(partition_by=interaction_table.c.session_id)
            .label("interaction_count"),
        )
        .distinct(interaction_table.c.session_id)
        .where(
            _owned(interaction_table, tenant_id, user_id),
            interaction_table.c.session_id.in_(session_ids),
        )
        .order_by(
            interaction_table.c.session_id,
            interaction_table.c.created_at.desc(),
            interaction_table.c.interaction_id.desc(),
        )
    )


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


def touch_interaction_run(
    tenant_id: str,
    user_id: str,
    interaction_id: str,
    *,
    last_event_sequence: int,
    updated_at: datetime,
) -> sa.Update:
    """Advance an informational commit's bookkeeping, never its lifecycle.

    Progress commits hand over an in-memory record whose ``status``/``state``
    are whatever this process last knew. Writing those columns would overwrite a
    terminal another process persisted in the meantime and resurrect a cancelled
    run, so only the sequence watermark and liveness timestamp move.
    """
    return (
        sa.update(interaction_table)
        .where(
            _owned(interaction_table, tenant_id, user_id),
            interaction_table.c.interaction_id == interaction_id,
        )
        .values(last_event_sequence=last_event_sequence, updated_at=updated_at)
    )


def fail_stale_interaction_run(
    tenant_id: str,
    user_id: str,
    interaction_id: str,
    *,
    stale_before: datetime,
    now: datetime,
    category: str,
) -> sa.Update:
    """Compare-and-set that fails an orphaned ``running`` interaction.

    A run is orphaned when its executor connection died without persisting a
    terminal event: the row still says ``running`` and ``updated_at`` stopped
    advancing. The update also reserves the terminal event sequence by bumping
    ``last_event_sequence``; the caller persists the terminal event itself.
    """
    return (
        sa.update(interaction_table)
        .where(
            _owned(interaction_table, tenant_id, user_id),
            interaction_table.c.interaction_id == interaction_id,
            interaction_table.c.status == "running",
            interaction_table.c.updated_at < stale_before,
        )
        .values(
            status="failed",
            state="failed",
            error_category=category,
            updated_at=now,
            completed_at=now,
            last_event_sequence=interaction_table.c.last_event_sequence + 1,
        )
        .returning(*interaction_table.c)
    )


def fail_stale_interaction_runs(
    *,
    stale_before: datetime,
    now: datetime,
    category: str,
) -> sa.Update:
    """Bulk startup variant of :func:`fail_stale_interaction_run`.

    Fails every stale ``running`` interaction across all owners in one
    compare-and-set; used only by the application-startup sweep, which is a
    process-boundary recovery job rather than a user-scoped query. It is
    therefore deliberately NOT listed in ``OWNERSHIP_SCOPED_BUILDERS``.
    """
    return (
        sa.update(interaction_table)
        .where(
            interaction_table.c.status == "running",
            interaction_table.c.updated_at < stale_before,
        )
        .values(
            status="failed",
            state="failed",
            error_category=category,
            updated_at=now,
            completed_at=now,
            last_event_sequence=interaction_table.c.last_event_sequence + 1,
        )
        .returning(*interaction_table.c)
    )


def fail_abandoned_interaction_runs(
    *,
    abandoned_before: datetime,
    now: datetime,
    category: str,
) -> sa.Update:
    """Bulk recovery: fail every ``pending`` interaction that never started.

    ``start`` persists a question with status ``pending`` and only the first
    claiming stream moves it to ``running``. A row whose creation is older than
    the abandonment threshold was never claimed, so it can never run and no
    stream-driven recovery would ever terminate it. Like
    :func:`fail_stale_interaction_runs` this is a process-boundary recovery job
    rather than a user-scoped query, so it is deliberately NOT listed in
    ``OWNERSHIP_SCOPED_BUILDERS``. The update also reserves the terminal event
    sequence by bumping ``last_event_sequence``; the caller persists the
    terminal event itself.
    """
    return (
        sa.update(interaction_table)
        .where(
            interaction_table.c.status == "pending",
            interaction_table.c.created_at < abandoned_before,
        )
        .values(
            status="failed",
            state="failed",
            error_category=category,
            updated_at=now,
            completed_at=now,
            last_event_sequence=interaction_table.c.last_event_sequence + 1,
        )
        .returning(*interaction_table.c)
    )


def delete_session(tenant_id: str, user_id: str, session_id: str) -> sa.Delete:
    return sa.delete(interaction_table).where(
        _owned(interaction_table, tenant_id, user_id),
        interaction_table.c.session_id == session_id,
    )


#: Statement builders that must always carry the ownership predicate.
#: ``insert_conversation`` is deliberately absent: an insert has no rows to
#: filter, it writes the trusted ownership pair as values (see its docstring).
OWNERSHIP_SCOPED_BUILDERS: tuple[str, ...] = (
    "select_interaction",
    "select_events",
    "select_messages",
    "select_latest_message",
    "select_interactions",
    "select_conversations",
    "select_conversation",
    "count_conversations",
    "select_conversation_messages",
    "select_conversation_first_questions",
    "select_conversation_interactions",
    "touch_conversation",
    "claim_interaction_run",
    "fail_stale_interaction_run",
    "delete_session",
)


__all__ = [
    "OWNERSHIP_SCOPED_BUILDERS",
    "CursorError",
    "claim_interaction_run",
    "count_conversations",
    "decode_cursor",
    "delete_session",
    "encode_cursor",
    "fail_abandoned_interaction_runs",
    "fail_stale_interaction_runs",
    "insert_conversation",
    "select_conversation",
    "select_conversation_first_questions",
    "select_conversation_interactions",
    "select_conversation_messages",
    "select_conversations",
    "select_events",
    "select_interaction",
    "select_interactions",
    "select_messages",
    "touch_conversation",
]
