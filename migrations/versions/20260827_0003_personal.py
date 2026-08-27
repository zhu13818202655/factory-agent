"""Query history, favorites, and minimal user mapping (Story 8).

History and favorites are ownership-filtered by the trusted ``(tenant_id,
user_id)`` pair exactly like sessions: a different user or a changed credential
must never read another person's old results. They store only normalized
non-sensitive slots — never raw question text, work numbers, or wage/output
amounts.

Revision ID: 20260827_0003_personal
Revises: 20260826_0002_artifact
Create Date: 2026-08-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0003_personal"
down_revision: str | None = "20260826_0002_artifact"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_user_mapping",
        sa.Column("uid", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("uname", sa.Text, nullable=False),
        sa.Column("company", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("uid", "tenant_id", name="agent_user_mapping_pk"),
    )

    op.create_table(
        "agent_query_history",
        sa.Column("history_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("capability_id", sa.Text, nullable=False),
        #: Normalized non-sensitive intent: time_expression + safe slot codes.
        sa.Column("intent", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "agent_query_history_owner_idx",
        "agent_query_history",
        ["tenant_id", "user_id", "created_at", "history_id"],
    )

    op.create_table(
        "agent_favorite",
        sa.Column("favorite_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("capability_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        #: Non-sensitive slots only; never ResultTable rows, wages, or scope IDs.
        sa.Column("slots", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "agent_favorite_owner_idx",
        "agent_favorite",
        ["tenant_id", "user_id", "favorite_id"],
    )
    op.create_index("agent_favorite_expiry_idx", "agent_favorite", ["expires_at"])


def downgrade() -> None:
    op.drop_index("agent_favorite_expiry_idx", table_name="agent_favorite")
    op.drop_index("agent_favorite_owner_idx", table_name="agent_favorite")
    op.drop_table("agent_favorite")
    op.drop_index("agent_query_history_owner_idx", table_name="agent_query_history")
    op.drop_table("agent_query_history")
    op.drop_table("agent_user_mapping")
