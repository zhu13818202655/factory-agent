"""Story 3: remove server-side artifact retention.

Drops ``agent_artifact``: exports are now 即时生成、直接下载、服务端不留存
(in-memory transient buffer only). The table recorded object-store metadata and
a 90-day lifecycle that no longer exists.

Revision ID: 20260903_0003_drop_artifact
Revises: 20260903_0002_scope_violation
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0003_drop_artifact"
down_revision: str | None = "20260903_0002_scope_violation"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_table("agent_artifact")


def downgrade() -> None:
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
