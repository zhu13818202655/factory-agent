"""usage-admin 数据库定稿基线（开发期单版本，Story 1-11 最终 schema）。

开发期（未上线）迁移历史于 2026-09-02 合并为单一版本：原
``20260827_0001_usage``（admin_audit）-> ``20260829_0002_tenant_registry``
（tenant_registry / platform_principal）-> ``20260901_0003_usage_export``
（usage_export）三条迭代合并为本文件。本文件是 ``versions/`` 下**唯一**的
迁移，本服务拥有并写入这四张表（Story 9 / 产品文档 §4.4 表归属）。

factory-agent 拥有并写入其业务表（``agent_*``）与全部计量表
（``usage_event`` / ``*_fact`` / ``mes_operation_category`` /
``tenant_usage_*``），建在 factory-agent 的单版本迁移
``20260824_0001_session`` 中，本服务对其**只读**，绝不建表、不改 schema。
计量写入由 factory-agent 在业务提交后的独立事务中执行（失败隔离），本服务
不再提供任何 ingest 写入接口。

后续交付客户后如需迭代开发，再按日期追加新版本（001、002、003 …），不再
改写本文件。

Revision ID: 20260827_0001_usage
Revises:
Create Date: 2026-08-27 (rewritten 2026-09-02 as single development baseline)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0001_usage"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # Admin audit trail: platform operation actions on tenant master data and
    # platform accounts.
    op.create_table(
        "admin_audit",
        sa.Column("audit_id", sa.Text, primary_key=True),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=True),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("admin_audit_principal_idx", "admin_audit", ["principal_id", "created_at"])

    # Tenant master data. app_key is the primary key (D12): the AppKey is
    # globally unique and itself the tenant identifier (M4), so the event
    # stream's tenant_id equals the primary key with zero mapping. status
    # drives the dashboard "status" column (D8), the factory-agent pre-call
    # guard (D13), and "disable instead of delete" (D10). AppKey is stored in
    # plaintext (D9) but every API response masks it.
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

    # Export jobs (POST /admin/v1/exports) are served by this service and write
    # this table, so it belongs here (Story 11 table split).
    op.create_table(
        "usage_export",
        sa.Column("export_id", sa.Text, primary_key=True),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("format", sa.Text, nullable=False),
        sa.Column("tenant_filter", sa.JSON, nullable=False),
        sa.Column("metric_version", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("artifact_key", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("usage_export_principal_idx", "usage_export", ["principal_id", "created_at"])


def downgrade() -> None:
    op.drop_index("usage_export_principal_idx", table_name="usage_export")
    op.drop_table("usage_export")
    op.drop_table("platform_principal")
    op.drop_index("tenant_registry_name_idx", table_name="tenant_registry")
    op.drop_index("tenant_registry_status_idx", table_name="tenant_registry")
    op.drop_table("tenant_registry")
    op.drop_index("admin_audit_principal_idx", table_name="admin_audit")
    op.drop_table("admin_audit")
