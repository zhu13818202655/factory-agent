"""SQLAlchemy Core table definitions for session state and metering tables.

Every business read and write is filtered by the trusted ``(tenant_id, user_id)``
ownership pair; there is deliberately no "by id only" access path.

Table ownership (ADR-0003): one service and one schema own every table here.
Business tables are the ``agent_*`` family; metering tables are ``usage_event``,
``interaction_fact``, ``llm_call_fact``, ``mes_call_fact``,
``mes_operation_category``, ``tenant_usage_hourly``, ``tenant_usage_daily``; and
the platform surface adds ``tenant_registry``, ``admin_audit``,
``platform_principal``, ``usage_export``. The Alembic migration history
(mirroring this metadata) is the only schema source in production; these
definitions drive the disposable test schema.
"""

import sqlalchemy as sa

METADATA = sa.MetaData()

interaction_table = sa.Table(
    "agent_interaction",
    METADATA,
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
    sa.Index(
        "agent_interaction_owner_idx",
        "tenant_id",
        "user_id",
        "session_id",
        "created_at",
        "interaction_id",
    ),
)

message_table = sa.Table(
    "agent_message",
    METADATA,
    sa.Column("message_id", sa.Text, primary_key=True),
    sa.Column(
        "interaction_id",
        sa.Text,
        sa.ForeignKey("agent_interaction.interaction_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False, server_default=""),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("interaction_id", "sequence", name="agent_message_sequence_key"),
    sa.Index(
        "agent_message_owner_idx",
        "tenant_id",
        "user_id",
        "session_id",
        "created_at",
        "message_id",
    ),
)

#: One row per conversation, so a conversation exists before its first
#: question. ``(tenant_id, user_id, session_id)`` is the primary key: the
#: ownership pair is part of the identity, so no query can address a
#: conversation without it. ``updated_at`` drives the history-panel ordering and
#: is indexed descending with the session id to keep the cursor page cheap.
conversation_table = sa.Table(
    "agent_conversation",
    METADATA,
    sa.Column("tenant_id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "agent_conversation_owner_idx",
        "tenant_id",
        "user_id",
        sa.text("updated_at DESC"),
        sa.text("session_id DESC"),
    ),
)

event_table = sa.Table(
    "agent_interaction_event",
    METADATA,
    sa.Column(
        "interaction_id",
        sa.Text,
        sa.ForeignKey("agent_interaction.interaction_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("sequence", sa.Integer, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("data", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

#: Monthly-partitioned raw usage event archive. The primary key is
#: ``(event_id, occurred_at)`` because ``event_id`` alone is not unique across
#: months and the partition key must be part of any unique constraint.
#: Production DDL (partition + helper) arrives through Alembic; this
#: flat definition only drives the disposable test schema.
usage_event_table = sa.Table(
    "usage_event",
    METADATA,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("schema_version", sa.Text, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("user_subject_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("interaction_id", sa.Text, nullable=False),
    sa.Column("trace_id", sa.Text, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Index("usage_event_tenant_occurred_idx", "tenant_id", "occurred_at"),
    sa.Index("usage_event_type_occurred_idx", "event_type", "occurred_at"),
)

interaction_fact_table = sa.Table(
    "interaction_fact",
    METADATA,
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
    sa.Index("interaction_fact_tenant_occurred_idx", "tenant_id", "occurred_at"),
)

llm_call_fact_table = sa.Table(
    "llm_call_fact",
    METADATA,
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
    sa.Index("llm_call_fact_tenant_occurred_idx", "tenant_id", "occurred_at"),
)

#: One row per customer MES HTTP call (success or failure), written by this
#: service at the adapter ``_send`` exit. ``page_count`` is
#: the page number within its paged fetch (1 for non-paged calls) and is never
#: summed into the call count (D6); call counts come from row counts.
mes_call_fact_table = sa.Table(
    "mes_call_fact",
    METADATA,
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
    sa.Index("mes_call_fact_tenant_occurred_idx", "tenant_id", "occurred_at"),
    sa.Index("mes_call_fact_operation_idx", "operation_id", "occurred_at"),
)

#: Reviewed ``operation_id`` → billing category mapping (D5). Owned by this
#: service, seeded from ``configs/knowledge/apis.yaml``; the category is applied
#: at aggregation time so a reclassification never rewrites event history.
mes_operation_category_table = sa.Table(
    "mes_operation_category",
    METADATA,
    sa.Column("operation_id", sa.Text, primary_key=True),
    sa.Column("category", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
)

tenant_usage_hourly_table = sa.Table(
    "tenant_usage_hourly",
    METADATA,
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("metric", sa.Text, nullable=False),
    sa.Column("value", sa.Float(), nullable=False),
    sa.Column("rollup_version", sa.Text, nullable=False),
    sa.Column("rolled_up_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "bucket_start", "metric"),
)

tenant_usage_daily_table = sa.Table(
    "tenant_usage_daily",
    METADATA,
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("bucket_date", sa.Date, nullable=False),
    sa.Column("metric", sa.Text, nullable=False),
    sa.Column("value", sa.Float(), nullable=False),
    sa.Column("rollup_version", sa.Text, nullable=False),
    sa.Column("rolled_up_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "bucket_date", "metric"),
)

user_mapping_table = sa.Table(
    "agent_user_mapping",
    METADATA,
    sa.Column("uid", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("uname", sa.Text, nullable=False),
    sa.Column("company", sa.Text, nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("uid", "tenant_id", name="agent_user_mapping_pk"),
)

query_history_table = sa.Table(
    "agent_query_history",
    METADATA,
    sa.Column("history_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("intent", sa.JSON, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "agent_query_history_owner_idx",
        "tenant_id",
        "user_id",
        "created_at",
        "history_id",
    ),
)

favorite_table = sa.Table(
    "agent_favorite",
    METADATA,
    sa.Column("favorite_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("slots", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("agent_favorite_owner_idx", "tenant_id", "user_id", "favorite_id"),
    sa.Index("agent_favorite_expiry_idx", "expires_at"),
)

#: Role-consistency review surface. Structured findings from the
#: consistency validator; never contains sensitive values — only digests and
#: counts. Both the real-time alert path and the periodic scope-review task
#: read/write it.
scope_violation_table = sa.Table(
    "agent_scope_violation",
    METADATA,
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
    sa.Index("agent_scope_violation_created_idx", "created_at"),
    sa.Index("agent_scope_violation_tenant_idx", "tenant_id", "created_at"),
)

#: Push subscription preferences: monthly/weekly cadence + selected
#: content items. The daily morning report is default-on and never stored here.
#: Non-sensitive only (dates/times/content-item ids).
user_preference_table = sa.Table(
    "agent_user_preference",
    METADATA,
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
)

#: Push delivery log. Records only the delivery
#: envelope — recipient, kind, item, status, and a message digest — never the
#: message body or business amounts.
push_delivery_table = sa.Table(
    "agent_push_delivery",
    METADATA,
    sa.Column("delivery_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("content_item_id", sa.Text, nullable=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("message_digest", sa.Text, nullable=True),
    sa.Column("row_count", sa.Integer, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("agent_push_delivery_created_idx", "created_at"),
    sa.Index("agent_push_delivery_owner_idx", "tenant_id", "user_id", "created_at"),
)

#: Tenant master data. ``app_key`` is the primary key: the AppKey is globally
#: unique and is itself the tenant identifier, so the metering stream's
#: ``tenant_id`` needs no mapping. ``status`` drives the dashboard column, the
#: business-edge admission guard, and "disable instead of delete". The AppKey is
#: stored in plaintext and masked in every outbound response; ``tenant_ref`` is
#: the non-secret handle operators and audit records address a tenant by, since
#: a masked AppKey is not unique across tenants.
tenant_registry_table = sa.Table(
    "tenant_registry",
    METADATA,
    sa.Column("app_key", sa.Text, primary_key=True),
    sa.Column("tenant_ref", sa.Text, nullable=False),
    sa.Column("tenant_name", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("tenant_ref", name="tenant_registry_tenant_ref_key"),
    sa.Index("tenant_registry_status_idx", "status"),
    sa.Index("tenant_registry_name_idx", "tenant_name"),
)

#: Platform operation actions on tenant master data and platform accounts.
admin_audit_table = sa.Table(
    "admin_audit",
    METADATA,
    sa.Column("audit_id", sa.Text, primary_key=True),
    sa.Column("principal_id", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target", sa.Text, nullable=True),
    sa.Column("detail", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("admin_audit_principal_idx", "principal_id", "created_at"),
)

#: Platform operations accounts, fully isolated from factory MES users.
#: Passwords are stored hashed; ``tenant_scope`` is an array of allowed AppKeys
#: (empty = all tenants).
platform_principal_table = sa.Table(
    "platform_principal",
    METADATA,
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

#: Export jobs served by the platform statistics surface, which writes this
#: table; only aggregate rows and the artifact key are stored.
usage_export_table = sa.Table(
    "usage_export",
    METADATA,
    sa.Column("export_id", sa.Text, primary_key=True),
    sa.Column("principal_id", sa.Text, nullable=False),
    sa.Column("format", sa.Text, nullable=False),
    sa.Column("tenant_filter", sa.JSON, nullable=False),
    sa.Column("metric_version", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("artifact_key", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("usage_export_principal_idx", "principal_id", "created_at"),
)

__all__ = [
    "METADATA",
    "admin_audit_table",
    "event_table",
    "favorite_table",
    "interaction_fact_table",
    "interaction_table",
    "llm_call_fact_table",
    "mes_call_fact_table",
    "mes_operation_category_table",
    "message_table",
    "platform_principal_table",
    "push_delivery_table",
    "query_history_table",
    "scope_violation_table",
    "tenant_registry_table",
    "tenant_usage_daily_table",
    "tenant_usage_hourly_table",
    "usage_event_table",
    "usage_export_table",
    "user_mapping_table",
    "user_preference_table",
]
