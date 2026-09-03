"""Add mock_employee.group_id for the role-01 (组长) group scope.

Story 1 "Mock MES 对齐" (决策点 (b)): a 小组 is an organisational attribute on
the employee master, not a separate dept record. The ``group`` payload field is
mirrored to this ``group_id`` column so row-level filtering can narrow the
role-01 view to the caller's group member uids directly in SQL.

Schema changes arrive only through Alembic; startup code never creates tables.

Revision ID: 20260903_0001_employee_group
Revises: 20260829_0001_mock_mes
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0001_employee_group"
down_revision: str | None = "20260829_0001_mock_mes"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("mock_employee", sa.Column("group_id", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("mock_employee", "group_id")
