from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260821_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seed_state",
        sa.Column("scenario", sa.String(length=32), primary_key=True),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("virtual_now", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
    )
    op.create_table(
        "identity_membership",
        sa.Column("membership_id", sa.String(length=128), primary_key=True),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("employee_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_membership_subject", "identity_membership", ["subject_id"])
    op.create_index("ix_membership_tenant", "identity_membership", ["tenant_id"])
    op.create_table(
        "canonical_resource",
        sa.Column("resource_type", sa.String(length=64), primary_key=True),
        sa.Column("resource_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_resource_tenant", "canonical_resource", ["tenant_id"])
    op.create_index("ix_resource_occurred", "canonical_resource", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("canonical_resource")
    op.drop_table("identity_membership")
    op.drop_table("seed_state")
