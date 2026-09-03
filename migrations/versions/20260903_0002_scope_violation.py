"""Story 2: role-consistency violation review surface.

Adds ``agent_scope_violation`` — structured, non-sensitive findings from the
role-consistency validator (exact/heuristic). Read/written by the real-time
alert path and the periodic scope-review task.

Revision ID: 20260903_0002_scope_violation
Revises: 20260824_0001_session
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0002_scope_violation"
down_revision: str | None = "20260824_0001_session"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_scope_violation",
        sa.Column("violation_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("capability_id", sa.Text, nullable=False),
        sa.Column("level", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("reason_code", sa.Text, nullable=False),
        sa.Column("interaction_id", sa.Text, nullable=True),
        sa.Column("expected_range", sa.Text, nullable=False),
        sa.Column("actual_summary", sa.Text, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("sample_count", sa.Integer, nullable=False),
        sa.Column("sample_digests", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "agent_scope_violation_created_idx",
        "agent_scope_violation",
        ["created_at"],
    )
    op.create_index(
        "agent_scope_violation_tenant_idx",
        "agent_scope_violation",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("agent_scope_violation_tenant_idx", table_name="agent_scope_violation")
    op.drop_index("agent_scope_violation_created_idx", table_name="agent_scope_violation")
    op.drop_table("agent_scope_violation")
