"""Session, message, event, and usage outbox baseline.

Revision ID: 20260824_0001_session
Revises:
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0001_session"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_interaction",
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
    )
    op.create_index(
        "agent_interaction_owner_idx",
        "agent_interaction",
        ["tenant_id", "user_id", "session_id", "created_at", "interaction_id"],
    )

    op.create_table(
        "agent_message",
        sa.Column("message_id", sa.Text, primary_key=True),
        sa.Column("interaction_id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["interaction_id"],
            ["agent_interaction.interaction_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("interaction_id", "sequence", name="agent_message_sequence_key"),
    )
    op.create_index(
        "agent_message_owner_idx",
        "agent_message",
        ["tenant_id", "user_id", "session_id", "created_at", "message_id"],
    )

    op.create_table(
        "agent_interaction_event",
        sa.Column("interaction_id", sa.Text, primary_key=True),
        sa.Column("sequence", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["interaction_id"],
            ["agent_interaction.interaction_id"],
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "usage_event_outbox",
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
    )
    op.create_index(
        "usage_event_outbox_pending_idx",
        "usage_event_outbox",
        ["published_at", "dead_lettered", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("usage_event_outbox_pending_idx", table_name="usage_event_outbox")
    op.drop_table("usage_event_outbox")
    op.drop_table("agent_interaction_event")
    op.drop_index("agent_message_owner_idx", table_name="agent_message")
    op.drop_table("agent_message")
    op.drop_index("agent_interaction_owner_idx", table_name="agent_interaction")
    op.drop_table("agent_interaction")
