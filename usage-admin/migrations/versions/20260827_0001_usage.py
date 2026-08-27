"""Usage metering storage baseline.

Creates the idempotent receipt, the monthly-partitioned raw event table, the
interaction/LLM fact tables, restricted dead-letter metadata, the hourly/daily
rollup tables, the export registry, and the admin audit log. Startup never runs
this code; schema changes arrive only through Alembic.

Revision ID: 20260827_0001_usage
Revises:
Create Date: 2026-08-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0001_usage"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # Idempotent receipt: event_id is the only deduplication authority. The
    # payload digest decides "same digest = safe redelivery" vs "different
    # digest = reject + alert". No sensitive payload is stored here.
    op.create_table(
        "usage_event_receipt",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("schema_version", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("payload_digest", sa.Text, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "usage_event_receipt_tenant_idx",
        "usage_event_receipt",
        ["tenant_id", "received_at"],
    )

    # Monthly-partitioned raw event table. Primary key is (event_id,
    # occurred_at) because event_id alone is not unique across months and the
    # partition key must be part of any unique constraint.
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

    # Partition helper. Missed partition maintenance is a documented ops
    # concern, so the helper is idempotent and exposed as an admin command.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION usage_admin_create_partition(target_month DATE)
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
    op.execute("SELECT usage_admin_create_partition(DATE '2026-08-01')")
    op.execute("SELECT usage_admin_create_partition(DATE '2026-09-01')")
    op.execute("SELECT usage_admin_create_partition(DATE '2026-10-01')")

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

    # Restricted dead-letter metadata only: no raw payload is ever persisted
    # for an unsupported or conflicting event.
    op.create_table(
        "usage_event_dead_letter",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("payload_digest", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False),
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


def downgrade() -> None:
    op.drop_index("admin_audit_principal_idx", table_name="admin_audit")
    op.drop_table("admin_audit")
    op.drop_table("usage_export")
    op.drop_table("tenant_usage_daily")
    op.drop_table("tenant_usage_hourly")
    op.drop_table("usage_event_dead_letter")
    op.drop_index("llm_call_fact_tenant_occurred_idx", table_name="llm_call_fact")
    op.drop_table("llm_call_fact")
    op.drop_index("interaction_fact_tenant_occurred_idx", table_name="interaction_fact")
    op.drop_table("interaction_fact")
    op.execute("DROP FUNCTION IF EXISTS usage_admin_create_partition(DATE)")
    op.execute("DROP TABLE IF EXISTS usage_event")
    op.drop_index("usage_event_receipt_tenant_idx", table_name="usage_event_receipt")
    op.drop_table("usage_event_receipt")
