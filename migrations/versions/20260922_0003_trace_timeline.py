"""调用链时间轴字段 + debug_trace 调试载荷表。

两件事同批落地，因为它们服务同一个读模型（``GET /v1/interactions/{id}/trace``）
且同属方案的第 3、5 步：

**一、``llm_call_fact`` / ``mes_call_fact`` 补时间轴与父子关系。**
原表只有 ``duration_ms``，而 ``occurred_at`` 是事件**构造时刻**（即调用结束时），
不是调用的起止。没有起点就没有 offset，瀑布图无从画起。本迁移补三列：

- ``started_at`` / ``ended_at``：调用的墙钟两端。可空——迁移前写入的行没有这个
  读数，而补一个猜测值会让它与真实测量无法区分。
- ``parent_span_id``：本轮调用所属的阶段 span（或外层 span）。``logical_call_id``
  是扁平 id，表达不了"第 2 轮的 EXTRACT 由第 1 轮结果触发"这类跨轮因果。

**二、新增 ``debug_trace``（B 方案调试通道，独立存储）。**
存事实表刻意排除的内容：prompt、工具入参、工具返回。独立成表而不是塞进
``usage_event.payload``，是为了让"内容留存"的保留期与访问控制在此表自己决定，
而不是从日志/计量链路继承。

- 不建外键指向 ``agent_interaction``：与 ``20260921_0002`` 的取舍一致——
  表间一致性由业务侧在同一事务内保证，加外键要改既有表且收益不足。
- ``trace_id`` 为 ``{interaction_id}:{span_key}``，确定性主键使重复提交只写一次。
- ``expires_at`` + 局部索引沿用 ``agent_favorite`` / ``usage_export`` 的
  "落盘 + 保留期 + 惰性清理"先例，默认由 ``debug_trace_retention_hours`` 决定。
- ``(tenant_id, user_id, interaction_id, span_key)`` 覆盖读取；所有权对进入索引，
  与 ``agent_interaction_owner_idx`` 同口径——不存在"按 id 单独寻址"的路径。

本迁移对既有数据是**纯增量**：三列可空、一张新表，不重写任何既有行。
``cert`` / ``staging`` / ``prod`` 下该表只减不增（不捕获即不写入）。

Revision ID: 20260922_0003_trace_timeline
Revises: 20260921_0002_conversations
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0003_trace_timeline"
down_revision: str | None = "20260921_0002_conversations"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

#: The two metering tables that gained a timeline.
_FACT_TABLES = ("llm_call_fact", "mes_call_fact")


def upgrade() -> None:
    for table in _FACT_TABLES:
        op.add_column(table, sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("parent_span_id", sa.Text, nullable=True))

    op.create_table(
        "debug_trace",
        sa.Column("trace_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("interaction_id", sa.Text, nullable=False),
        sa.Column("span_key", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("stage", sa.Text, nullable=True),
        sa.Column("logical_call_id", sa.Text, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=True),
        sa.Column("operation_id", sa.Text, nullable=True),
        sa.Column("input_payload", sa.JSON, nullable=True),
        sa.Column("output_payload", sa.JSON, nullable=True),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("original_rows", sa.Integer, nullable=True),
        sa.Column("original_bytes", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "debug_trace_owner_idx",
        "debug_trace",
        ["tenant_id", "user_id", "interaction_id", "span_key"],
    )
    op.create_index("debug_trace_expiry_idx", "debug_trace", ["expires_at"])


def downgrade() -> None:
    op.drop_index("debug_trace_expiry_idx", table_name="debug_trace")
    op.drop_index("debug_trace_owner_idx", table_name="debug_trace")
    op.drop_table("debug_trace")
    for table in _FACT_TABLES:
        op.drop_column(table, "parent_span_id")
        op.drop_column(table, "ended_at")
        op.drop_column(table, "started_at")
