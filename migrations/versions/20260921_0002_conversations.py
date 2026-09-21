"""新增 agent_conversation 表：会话成为一等实体。

背景：会话此前是隐式的——某个 ``session_id`` 的第一条 ``agent_interaction``
行就是它存在的唯一证据，因此「建了会话但还没提问」的空会话无法出现在历史
列表里，标题每次都要回读消息表。本迁移把会话提升为独立实体，供前端历史
面板的列表、详情与排序使用。

设计要点：

- 主键 ``(tenant_id, user_id, session_id)``：所有权对本身就是身份的一部分，
  不存在「按 session_id 单独寻址」的读取路径。
- 不建外键指向 ``agent_interaction``：那要改动既有表，收益不足。一致性由
  业务侧保证——会话行与 interaction 在**同一事务**内写入，且每次 interaction
  落库都刷新 ``updated_at``。
- ``agent_conversation_owner_idx`` 覆盖列表查询的过滤与排序
  （``ORDER BY updated_at DESC, session_id DESC``）。
- 该表属业务表（``agent_*`` 族），不参与计量链路，建档不产生 usage 事件。

本文件按 ``20260917_0001_init`` 自述的约定追加，不改写基线。

Revision ID: 20260921_0002_conversations
Revises: 20260917_0001_init
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0002_conversations"
down_revision: str | None = "20260917_0001_init"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("session_id", sa.Text, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "agent_conversation_owner_idx",
        "agent_conversation",
        ["tenant_id", "user_id", sa.text("updated_at DESC"), sa.text("session_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("agent_conversation_owner_idx", table_name="agent_conversation")
    op.drop_table("agent_conversation")
