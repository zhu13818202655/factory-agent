"""消息索引尾列对齐排序键：message_id → (interaction_id, sequence)。

``agent_message`` 的读取排序键是 ``(created_at, interaction_id, sequence)``
（见 ``queries.select_messages``），而 ``agent_message_owner_idx`` 的尾列还是
``message_id``。``message_id`` 是随机 UUID：一次 commit 里同微秒落库的多条
消息（结果卡片与最终回答）在索引里彼此无序，查询必须对这一小段再排序一次。
索引尾列改成排序键的后两层后，扫描顺序与输出顺序完全一致。

只动索引、不动任何列与行：PostgreSQL 的 ``DROP INDEX`` + ``CREATE INDEX``
对既有数据是纯元数据操作，不重写表。同一规模下（单会话几十条消息）两种形状
的实测差异都在噪声内，本次落地只为让"索引顺序 = 查询顺序"这一不变量成立，
避免未来消息量级上来后再排查。

Revision ID: 20260923_0004_msg_order_idx
Revises: 20260922_0003_trace_timeline
Create Date: 2026-09-23
"""

from alembic import op

revision: str = "20260923_0004_msg_order_idx"
down_revision: str | None = "20260922_0003_trace_timeline"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_INDEX_NAME = "agent_message_owner_idx"
_OLD_COLUMNS = ["tenant_id", "user_id", "session_id", "created_at", "message_id"]
_NEW_COLUMNS = [
    "tenant_id",
    "user_id",
    "session_id",
    "created_at",
    "interaction_id",
    "sequence",
]


def upgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="agent_message")
    op.create_index(_INDEX_NAME, "agent_message", _NEW_COLUMNS)


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="agent_message")
    op.create_index(_INDEX_NAME, "agent_message", _OLD_COLUMNS)
