"""V2 接口换代的计费分类补种：GongziClQuery → payroll。

``configs/knowledge/apis.yaml`` 新增工资产量 V2 操作 ``GongziClQuery``
（``usage_category: payroll``，基线种子同步补行）。已迁移过的开发库通过本
迁移补齐映射行：``mes_call_fact`` 聚合在计费时按 ``mes_operation_category``
归类，缺行会让该操作的调用量从计费统计中静默消失（D5 红线）。

``ON CONFLICT DO NOTHING`` 让「已由基线种子插入的全新库」与本迁移幂等；
``downgrade`` 只删本行，不动任何其他分类。

Revision ID: 20261011_0005_gongzi_cl_category
Revises: 20260923_0004_msg_order_idx
Create Date: 2026-10-11
"""

from alembic import op

revision: str = "20261011_0005_gongzi_cl_category"
down_revision: str | None = "20260923_0004_msg_order_idx"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO mes_operation_category (operation_id, category, version)
        VALUES ('GongziClQuery', 'payroll', 'apis-v2')
        ON CONFLICT (operation_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM mes_operation_category WHERE operation_id = 'GongziClQuery'")
