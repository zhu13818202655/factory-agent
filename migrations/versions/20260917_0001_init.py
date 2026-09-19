"""数据库定稿基线（开发期单版本，最终 schema）。

开发期（未上线）迁移历史于 2026-09-17 合并为单一版本：本文件是
``migrations/versions/`` 下**唯一**的迁移，内容为合并前迭代链叠加后的净建表
结果，``down_revision`` 为空。

合并为单版本，顺带修掉三处既有缺陷：
- ``agent_artifact`` 曾在早期版本建表、又在后续版本删除，净效果是「表不存在」；
  重写后它只存在于 git 历史，不再有「降级复活死表」的路径。
- ``usage_event`` 分区的种子行原来写死 ``2026-08 / 09 / 10``，而
  ``factory_agent_create_partition()`` 在应用代码里没有调用者，跨月即静默
  丢账；这里改为按迁移执行时刻动态取「当月 + 下月」，并由运行时周期任务
  （``factory_agent.statistics.partitions``）保证后续月份始终存在。
- ``mes_operation_category`` 的种子行数注释统一为 30（与
  ``configs/knowledge/apis.yaml`` 的 30 条一致）。

包含的表：
- 会话：``agent_interaction`` / ``agent_message``（含
  ``agent_message_sequence_key`` 唯一约束） / ``agent_interaction_event``
- 治理：``agent_scope_violation``
- 推送：``agent_user_preference`` / ``agent_push_delivery``
- 个性化：``agent_user_mapping`` / ``agent_query_history`` / ``agent_favorite``
- 计量：按月分区 ``usage_event`` + ``factory_agent_create_partition()``、
  事实表 ``interaction_fact`` / ``llm_call_fact`` / ``mes_call_fact``、
  分类映射 ``mes_operation_category``（30 行种子）、汇总
  ``tenant_usage_hourly`` / ``tenant_usage_daily``
- 平台：``tenant_registry``（``app_key`` 主键 + 非密 ``tenant_ref`` 唯一列） /
  ``admin_audit`` / ``platform_principal`` / ``usage_export``

计量写入口径：业务数据先提交，计量在业务提交后的**独立**事务中直写；
``usage_event`` 与其 ``*_fact`` 在同一计量事务内原子写入；计量失败仅告警，
绝不回滚业务、不阻塞问答。

后续交付客户后如需迭代开发，再按日期追加新版本（001、002、003 …），不再
改写本文件。

Revision ID: 20260917_0001_init
Revises:
Create Date: 2026-09-17 (rewritten as the single development baseline)
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0001_init"
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

    # --- Role-consistency review surface -----------------------------------
    # Structured, non-sensitive findings from the role-consistency validator.
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

    # --- Push preferences and delivery log ---------------------------------
    # The daily morning report is default-on and never stored; only monthly and
    # weekly cadence plus the selected content items live here. Nothing in this
    # pair is sensitive: dates, times, content-item ids, envelope status, and a
    # digest of the delivered message — never its body or business amounts.
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

    # --- Metering tables ----------------------------------------------------
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
    # Seed the current and following months at migration time so the first
    # writes land; ``ensure_usage_partitions`` keeps the window rolling from
    # then on. Both are needed: a hard-coded seed silently drops every write
    # once the window is passed, and the runtime task alone cannot cover the
    # very first write on a fresh database.
    op.execute("SELECT factory_agent_create_partition(date_trunc('month', now())::date)")
    op.execute(
        "SELECT factory_agent_create_partition("
        "(date_trunc('month', now()) + interval '1 month')::date)"
    )

    # interaction_fact / llm_call_fact / mes_call_fact are written in the same
    # metering transaction as the archive row (business commit already
    # happened). page_count is a supporting metric and is never summed into the
    # call count; call counts come from row counts.
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

    # Reviewed operation_id -> billing category mapping, seeded from
    # configs/knowledge/apis.yaml. Classification is applied at aggregation time
    # so a reclassification never rewrites history. The 30 rows below mirror the
    # `usage_category` fields in apis.yaml (guarded by
    # tests/unit/data_api/test_mes_operation_categories.py): a new operation
    # added to apis.yaml fails that test until it is classified here.
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
            ('ScjdQuery', 'order', 'apis-v2'),
            ('ScjdDetailQuery', 'order', 'apis-v2'),
            ('ScjdGxQuery', 'order', 'apis-v2'),
            ('ScjdFzHzQuery', 'order', 'apis-v2'),
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

    # --- Platform tables ----------------------------------------------------
    # Tenant master data. app_key is the primary key: the AppKey is globally
    # unique and is itself the tenant identifier, so the metering stream's
    # tenant_id needs no mapping. tenant_ref is the non-secret handle that
    # management endpoints and the audit trail address a tenant by — a masked
    # AppKey is not unique, so it can neither address nor distinguish a tenant.
    # The AppKey is stored in plaintext but every API response masks it.
    op.create_table(
        "tenant_registry",
        sa.Column("app_key", sa.Text, primary_key=True),
        sa.Column("tenant_ref", sa.Text, nullable=False),
        sa.Column("tenant_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_ref", name="tenant_registry_tenant_ref_key"),
    )
    op.create_index("tenant_registry_status_idx", "tenant_registry", ["status"])
    op.create_index("tenant_registry_name_idx", "tenant_registry", ["tenant_name"])

    # Admin audit trail: platform actions on tenant master data and accounts.
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

    # Platform operations accounts, fully isolated from factory MES users.
    # Passwords are stored hashed; tenant_scope is an array of allowed AppKeys
    # (empty = all tenants).
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

    # Export jobs; only aggregate rows and the artifact key are stored.
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
    # Reverse of the upgrade, most recently created tables first.
    op.drop_index("usage_export_principal_idx", table_name="usage_export")
    op.drop_table("usage_export")
    op.drop_table("platform_principal")
    op.drop_index("admin_audit_principal_idx", table_name="admin_audit")
    op.drop_table("admin_audit")
    op.drop_index("tenant_registry_name_idx", table_name="tenant_registry")
    op.drop_index("tenant_registry_status_idx", table_name="tenant_registry")
    op.drop_table("tenant_registry")
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
    op.drop_index("agent_push_delivery_owner_idx", table_name="agent_push_delivery")
    op.drop_index("agent_push_delivery_created_idx", table_name="agent_push_delivery")
    op.drop_table("agent_push_delivery")
    op.drop_table("agent_user_preference")
    op.drop_index("agent_scope_violation_tenant_idx", table_name="agent_scope_violation")
    op.drop_index("agent_scope_violation_created_idx", table_name="agent_scope_violation")
    op.drop_table("agent_scope_violation")
    op.drop_table("agent_interaction_event")
    op.drop_index("agent_message_owner_idx", table_name="agent_message")
    op.drop_table("agent_message")
    op.drop_index("agent_interaction_owner_idx", table_name="agent_interaction")
    op.drop_table("agent_interaction")
