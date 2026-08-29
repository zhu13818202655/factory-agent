"""Tenant master data and platform principal accounts.

Story 9: this service owns and writes ``tenant_registry`` (factory name +
AppKey + status) and ``platform_principal`` (operations accounts). Per the
table-ownership breaking change (product doc 4.4) this migration directory
only ever contains these two tables plus ``admin_audit`` (already created in
``20260827_0001_usage``); every metering table's DDL lives in the
factory-agent migration history and is read-only here.

``tenant_registry`` uses ``app_key`` directly as the primary key (D12): the
AppKey is globally unique and itself the tenant identifier (M4), so the event
stream's ``tenant_id`` equals the primary key with zero mapping.

Revision ID: 20260829_0002_tenant_registry
Revises: 20260827_0001_usage
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0002_tenant_registry"
down_revision: str | None = "20260827_0001_usage"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # Tenant master data. app_key is the primary key (D12); status drives the
    # dashboard "status" column (D8), the factory-agent pre-call guard (D13),
    # and "disable instead of delete" (D10). AppKey is stored in plaintext
    # (D9) but every API response masks it.
    op.create_table(
        "tenant_registry",
        sa.Column("app_key", sa.Text, primary_key=True),
        sa.Column("tenant_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "tenant_registry_status_idx",
        "tenant_registry",
        ["status"],
    )
    op.create_index(
        "tenant_registry_name_idx",
        "tenant_registry",
        ["tenant_name"],
    )

    # Platform operations accounts (D15): internal-only, fully isolated from
    # factory MES users. Passwords are stored hashed; tenant_scope is a JSONB
    # array of allowed AppKeys (empty = all tenants).
    op.create_table(
        "platform_principal",
        sa.Column("principal_id", sa.Text, primary_key=True),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("tenant_scope", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username", name="platform_principal_username_key"),
    )


def downgrade() -> None:
    op.drop_table("platform_principal")
    op.drop_index("tenant_registry_name_idx", table_name="tenant_registry")
    op.drop_index("tenant_registry_status_idx", table_name="tenant_registry")
    op.drop_table("tenant_registry")
