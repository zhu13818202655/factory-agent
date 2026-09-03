"""Story 3B migration: push preferences and push-delivery log.

Adds ``agent_user_preference`` (monthly/weekly cadence + content items; the
daily morning report is default-on and never stored) and ``agent_push_delivery``
(local fake channel envelope log — never message content).

Revision ID: 20260903_0004_push
Revises: 20260903_0003_drop_artifact
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0004_push"
down_revision: str | None = "20260903_0003_drop_artifact"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_user_preference",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("weekly_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("weekly_day_of_week", sa.Integer, nullable=True),
        sa.Column("weekly_time", sa.Text, nullable=True),
        sa.Column("monthly_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("monthly_day_of_month", sa.Integer, nullable=True),
        sa.Column("monthly_time", sa.Text, nullable=True),
        sa.Column("content_items", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="agent_user_preference_pkey"),
    )
    op.create_table(
        "agent_push_delivery",
        sa.Column("delivery_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("content_item_id", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("message_digest", sa.Text, nullable=True),
        sa.Column("row_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "agent_push_delivery_created_idx",
        "agent_push_delivery",
        ["created_at"],
    )
    op.create_index(
        "agent_push_delivery_owner_idx",
        "agent_push_delivery",
        ["tenant_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("agent_push_delivery_owner_idx", table_name="agent_push_delivery")
    op.drop_index("agent_push_delivery_created_idx", table_name="agent_push_delivery")
    op.drop_table("agent_push_delivery")
    op.drop_table("agent_user_preference")
