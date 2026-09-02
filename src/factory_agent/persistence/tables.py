"""SQLAlchemy Core table definitions for session state and metering tables.

Every business read and write is filtered by the trusted ``(tenant_id, user_id)``
ownership pair; there is deliberately no "by id only" access path.

Table ownership (Story 11, product doc 4.4): this service owns every
``agent_*`` business table plus all metering tables (``usage_event``,
``interaction_fact``, ``llm_call_fact``, ``mes_call_fact``,
``mes_operation_category``, ``tenant_usage_hourly``, ``tenant_usage_daily``).
``tenant_registry`` / ``admin_audit`` / ``platform_principal`` / ``usage_export``
are owned by usage-admin and never declared here. The Alembic migration history
(mirroring this metadata) is the only schema source in production; these
definitions drive the disposable test schema.
"""

from __future__ import annotations

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
#: months and the partition key must be part of any unique constraint (Story
#: 11 1.2). Production DDL (partition + helper) arrives through Alembic; this
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
#: service at the adapter ``_send`` exit (Story 11 2.4/2.6). ``page_count`` is
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

artifact_table = sa.Table(
    "agent_artifact",
    METADATA,
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
    sa.Index("agent_artifact_owner_idx", "tenant_id", "user_id", "artifact_id"),
    sa.Index("agent_artifact_expiry_idx", "expires_at"),
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

__all__ = [
    "METADATA",
    "artifact_table",
    "event_table",
    "favorite_table",
    "interaction_fact_table",
    "interaction_table",
    "llm_call_fact_table",
    "mes_call_fact_table",
    "mes_operation_category_table",
    "message_table",
    "query_history_table",
    "tenant_usage_daily_table",
    "tenant_usage_hourly_table",
    "usage_event_table",
    "user_mapping_table",
]
