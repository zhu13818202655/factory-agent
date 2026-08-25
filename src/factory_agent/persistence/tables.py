"""SQLAlchemy Core table definitions for session state and the usage outbox.

Every business read and write is filtered by the trusted ``(tenant_id, user_id)``
ownership pair; there is deliberately no "by id only" access path.
"""

from __future__ import annotations

import sqlalchemy as sa

METADATA = sa.MetaData()

interaction_table = sa.Table(
    "agent_interaction",
    METADATA,
    sa.Column("interaction_id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("input_text", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=True),
    sa.Column("clarification_rounds", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_event_sequence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error_category", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Index(
        "agent_interaction_owner_idx",
        "tenant_id",
        "user_id",
        "session_id",
        "created_at",
        "interaction_id",
    ),
)

message_table = sa.Table(
    "agent_message",
    METADATA,
    sa.Column("message_id", sa.Text, primary_key=True),
    sa.Column(
        "interaction_id",
        sa.Text,
        sa.ForeignKey("agent_interaction.interaction_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False, server_default=""),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("interaction_id", "sequence", name="agent_message_sequence_key"),
    sa.Index(
        "agent_message_owner_idx",
        "tenant_id",
        "user_id",
        "session_id",
        "created_at",
        "message_id",
    ),
)

event_table = sa.Table(
    "agent_interaction_event",
    METADATA,
    sa.Column(
        "interaction_id",
        sa.Text,
        sa.ForeignKey("agent_interaction.interaction_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("sequence", sa.Integer, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("data", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

usage_outbox_table = sa.Table(
    "usage_event_outbox",
    METADATA,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("dead_lettered", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("dead_letter_reason", sa.Text, nullable=True),
    sa.Index("usage_event_outbox_pending_idx", "published_at", "dead_lettered", "available_at"),
)

__all__ = [
    "METADATA",
    "event_table",
    "interaction_table",
    "message_table",
    "usage_outbox_table",
]
