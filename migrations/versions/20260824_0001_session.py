"""factory-agent 数据库定稿基线（开发期单版本，最终 schema）。

开发期（未上线）迁移历史于 2026-09-02 合并为单一版本：本文件是
``migrations/versions/`` 下**唯一**的迁移，内容为原 ``0001_session`` ->
``0002_artifact`` -> ``0003_personal`` -> ``0004_metering`` 迭代链的净建表
结果。outbox 移除与表归属拆分已整体融入初始态：
``usage_event_outbox`` / ``usage_event_receipt`` / ``usage_event_dead_letter``
从未出现在本历史中；``tenant_registry`` / ``admin_audit`` /
``platform_principal`` / ``usage_export`` 归 usage-admin 拥有，建在
usage-admin 的单版本迁移 ``20260827_0001_usage`` 中，本文件绝不触碰。

后续交付客户后如需迭代开发，再按日期追加新版本（001、002、003 …），不再
改写本文件。

包含的表：
- 业务表：``agent_interaction`` / ``agent_message`` /
  ``agent_interaction_event``、``agent_artifact``、
  ``agent_user_mapping`` / ``agent_query_history`` / ``agent_favorite``
- 计量表（同库直写，本服务拥有并写入）：按月分区 ``usage_event``、
  事实表 ``interaction_fact`` / ``llm_call_fact`` / ``mes_call_fact``、
  分类映射 ``mes_operation_category``（27 行种子）、汇总
  ``tenant_usage_hourly`` / ``tenant_usage_daily``。

计量写入口径：业务数据先提交，计量在业务提交后的**独立
事务**中直写；``usage_event`` 与其 ``*_fact`` 在同一计量事务内原子写入；
计量失败仅告警，绝不回滚业务、不阻塞问答。

Revision ID: 20260824_0001_session
Revises:
Create Date: 2026-08-24 (rewritten 2026-09-02 as single development baseline)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0001_session"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # --- Session / message / interaction-event baseline --------------------
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

    # --- Artifact metadata --------------------------------------------------
    # Artifact content lives in object storage; this table records only the
    # opaque object key plus the tenant/owning user, capability, filename, size,
    # SHA-256, and retention timestamps. It never stores employee IDs, names,
    # question text, W2 amounts, or any sensitive field.
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

    # --- User mapping / query history / favorites ---------------------------
    # History and favorites are ownership-filtered by the trusted
    # (tenant_id, user_id) pair exactly like sessions; they store only
    # normalized non-sensitive slots.
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

    # --- Metering tables (direct-write, owned by this service) --------------
    # Raw archive, partitioned by month. Primary key is (event_id,
    # occurred_at) because event_id alone is not unique across months and the
    # partition key must be part of any unique constraint. Writes use
    # ON CONFLICT DO NOTHING so a repeated event is recorded exactly once.
    op.execute(
        """
        CREATE TABLE usage_event (
            event_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            event_type TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL,
            user_subject_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            interaction_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            payload JSONB NOT NULL,
            PRIMARY KEY (event_id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.create_index(
        "usage_event_tenant_occurred_idx",
        "usage_event",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "usage_event_type_occurred_idx",
        "usage_event",
        ["event_type", "occurred_at"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION factory_agent_create_partition(target_month DATE)
        RETURNS void AS $$
        DECLARE
            partition_name TEXT;
            start_date DATE;
            end_date DATE;
        BEGIN
            partition_name := 'usage_event_' || to_char(target_month, 'YYYYMM');
            start_date := date_trunc('month', target_month)::date;
            end_date := (start_date + interval '1 month')::date;
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF usage_event '
                'FOR VALUES FROM (%L) TO (%L)',
                partition_name, start_date, end_date
            );
        END;
        $$ LANGUAGE plpgsql
        """
    )
    # Seed partitions for the current and following months so first writes land.
    op.execute("SELECT factory_agent_create_partition(DATE '2026-08-01')")
    op.execute("SELECT factory_agent_create_partition(DATE '2026-09-01')")
    op.execute("SELECT factory_agent_create_partition(DATE '2026-10-01')")

    # interaction_fact / llm_call_fact are written in the same metering
    # transaction as the archive row (business commit already happened), with
    # the same fields and semantics as before.
    op.create_table(
        "interaction_fact",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("interaction_id", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("user_subject_id", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capability_id", sa.Text, nullable=True),
        sa.Column("entrypoint", sa.Text, nullable=True),
        sa.Column("role_category", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.BigInteger, nullable=True),
        sa.Column("mes_duration_ms", sa.BigInteger, nullable=True),
        sa.Column("llm_duration_ms", sa.BigInteger, nullable=True),
        sa.Column("local_duration_ms", sa.BigInteger, nullable=True),
        sa.Column("result_rows_bucket", sa.Text, nullable=True),
        sa.Column("error_category", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "interaction_fact_tenant_occurred_idx",
        "interaction_fact",
        ["tenant_id", "occurred_at"],
    )

    op.create_table(
        "llm_call_fact",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("interaction_id", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("logical_call_id", sa.Text, nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("model_alias", sa.Text, nullable=False),
        sa.Column("actual_model", sa.Text, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger, nullable=False),
        sa.Column("completion_tokens", sa.BigInteger, nullable=False),
        sa.Column("cached_tokens", sa.BigInteger, nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger, nullable=False),
        sa.Column("duration_ms", sa.BigInteger, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("fallback_reason", sa.Text, nullable=True),
        sa.Column("error_category", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "llm_call_fact_tenant_occurred_idx",
        "llm_call_fact",
        ["tenant_id", "occurred_at"],
    )

    # MES call facts: one row per customer MES HTTP call (success or failure).
    # page_count is a supporting metric, never summed into the call count (D6);
    # call counts come from row counts.
    op.create_table(
        "mes_call_fact",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("interaction_id", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation_id", sa.Text, nullable=False),
        sa.Column("page_count", sa.Integer, nullable=False),
        sa.Column("row_count_bucket", sa.Text, nullable=False),
        sa.Column("duration_ms", sa.BigInteger, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_category", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "mes_call_fact_tenant_occurred_idx",
        "mes_call_fact",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "mes_call_fact_operation_idx",
        "mes_call_fact",
        ["operation_id", "occurred_at"],
    )

    # Reviewed operation_id -> billing category mapping (D5), owned by this
    # service and seeded from configs/knowledge/apis.yaml. Classification is
    # applied at aggregation time so a reclassification never rewrites history.
    # The 27 rows below mirror the `usage_category` fields in apis.yaml (verified
    # by tests/unit/.../test_mes_operation_categories.py); a new
    # operation added to apis.yaml fails that test until it is classified here.
    op.create_table(
        "mes_operation_category",
        sa.Column("operation_id", sa.Text, primary_key=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
    )
    op.execute(
        """
        INSERT INTO mes_operation_category (operation_id, category, version)
        VALUES
            ('SystemToken', 'other', 'apis-v2'),
            ('QuerySign', 'other', 'apis-v2'),
            ('TestPermissions', 'other', 'apis-v2'),
            ('UserInfoQuery', 'other', 'apis-v2'),
            ('MoveMenuQuery', 'other', 'apis-v2'),
            ('HuohaoQuery', 'other', 'apis-v2'),
            ('HuohaoFormQuery', 'other', 'apis-v2'),
            ('ScTypeQuery', 'other', 'apis-v2'),
            ('RfidWorktypeQuery', 'other', 'apis-v2'),
            ('HuohaoWorktypeQuery', 'other', 'apis-v2'),
            ('EmployeeQuery', 'other', 'apis-v2'),
            ('DeptQuery', 'other', 'apis-v2'),
            ('PlanGridPageList', 'order', 'apis-v2'),
            ('SclzdGridPageList', 'order', 'apis-v2'),
            ('SclzdWorktypeQuery', 'order', 'apis-v2'),
            ('SclzdBarcodeQuery', 'order', 'apis-v2'),
            ('BarcodeClQuery', 'output', 'apis-v2'),
            ('HuohaoWtCLQuery', 'output', 'apis-v2'),
            ('PinFengGridPageList', 'output', 'apis-v2'),
            ('WorktypeProgressQuery', 'output', 'apis-v2'),
            ('YskQuery', 'output', 'apis-v2'),
            ('WskQuery', 'output', 'apis-v2'),
            ('GongziMxQuery', 'payroll', 'apis-v2'),
            ('GongziJeOrderQuery', 'payroll', 'apis-v2'),
            ('DgGridPageList', 'other', 'apis-v2'),
            ('DgZuGridPageList', 'other', 'apis-v2'),
            ('DgClQuery', 'other', 'apis-v2')
        ON CONFLICT (operation_id) DO NOTHING
        """
    )

    # Rollup output, owned by this service; usage-admin only reads it.
    op.create_table(
        "tenant_usage_hourly",
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.Text, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("rollup_version", sa.Text, nullable=False),
        sa.Column("rolled_up_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "bucket_start", "metric"),
    )
    op.create_table(
        "tenant_usage_daily",
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("bucket_date", sa.Date, nullable=False),
        sa.Column("metric", sa.Text, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("rollup_version", sa.Text, nullable=False),
        sa.Column("rolled_up_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "bucket_date", "metric"),
    )


def downgrade() -> None:
    # Reverse of the upgrade, most recently created tables first.
    op.drop_table("tenant_usage_daily")
    op.drop_table("tenant_usage_hourly")
    op.drop_table("mes_operation_category")
    op.drop_index("mes_call_fact_operation_idx", table_name="mes_call_fact")
    op.drop_index("mes_call_fact_tenant_occurred_idx", table_name="mes_call_fact")
    op.drop_table("mes_call_fact")
    op.drop_index("llm_call_fact_tenant_occurred_idx", table_name="llm_call_fact")
    op.drop_table("llm_call_fact")
    op.drop_index("interaction_fact_tenant_occurred_idx", table_name="interaction_fact")
    op.drop_table("interaction_fact")
    op.execute("DROP FUNCTION IF EXISTS factory_agent_create_partition(DATE)")
    op.execute("DROP TABLE IF EXISTS usage_event")
    op.drop_index("agent_favorite_expiry_idx", table_name="agent_favorite")
    op.drop_index("agent_favorite_owner_idx", table_name="agent_favorite")
    op.drop_table("agent_favorite")
    op.drop_index("agent_query_history_owner_idx", table_name="agent_query_history")
    op.drop_table("agent_query_history")
    op.drop_table("agent_user_mapping")
    op.drop_index("agent_artifact_expiry_idx", table_name="agent_artifact")
    op.drop_index("agent_artifact_owner_idx", table_name="agent_artifact")
    op.drop_table("agent_artifact")
    op.drop_table("agent_interaction_event")
    op.drop_index("agent_message_owner_idx", table_name="agent_message")
    op.drop_table("agent_message")
    op.drop_index("agent_interaction_owner_idx", table_name="agent_interaction")
    op.drop_table("agent_interaction")
