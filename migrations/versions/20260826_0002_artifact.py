"""Artifact metadata table for exported results (Story 6).

Artifact content lives in object storage; this table records only the opaque
object key plus the tenant/owning user, capability, filename, size, SHA-256, and
retention timestamps. It never stores employee IDs, names, question text, W2
amounts, or any sensitive field.

Revision ID: 20260826_0002_artifact
Revises: 20260824_0001_session
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0002_artifact"
down_revision: str | None = "20260824_0001_session"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_artifact",
        sa.Column("artifact_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("interaction_id", sa.Text, nullable=False),
        sa.Column("capability_id", sa.Text, nullable=False),
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "agent_artifact_owner_idx",
        "agent_artifact",
        ["tenant_id", "user_id", "artifact_id"],
    )
    op.create_index("agent_artifact_expiry_idx", "agent_artifact", ["expires_at"])


def downgrade() -> None:
    op.drop_index("agent_artifact_expiry_idx", table_name="agent_artifact")
    op.drop_index("agent_artifact_owner_idx", table_name="agent_artifact")
    op.drop_table("agent_artifact")
