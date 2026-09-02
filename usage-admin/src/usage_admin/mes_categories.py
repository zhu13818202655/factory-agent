"""MES API classification for usage reporting (D1/D5).

The billing-relevant statistic is the number of customer MES API calls grouped
by API business category — not by agent capability. The authoritative
source of the mapping is ``mes_operation_category`` (owned by factory-agent,
read-only here, seeded from ``configs/knowledge/apis.yaml``); this module also
carries the reviewed default mapping so the service behaves correctly when that
table is not yet migrated, and so tests can run offline.

Categories: ``output`` (产量查询) / ``payroll`` (工资查询) / ``order`` (订单进度)
/ ``other`` (其余 15 个：认证 3 + 基础数据 9 + 吊挂 3, D5). The four values sum
to the total MES call count and are displayed side by side (D11).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from usage_admin.store import MesOperationCategory, UsageStore

CATEGORY_OUTPUT = "output"
CATEGORY_PAYROLL = "payroll"
CATEGORY_ORDER = "order"
CATEGORY_OTHER = "other"

#: Stable ordering for API responses and exports.
CATEGORY_ORDERING: tuple[str, ...] = (
    CATEGORY_OUTPUT,
    CATEGORY_PAYROLL,
    CATEGORY_ORDER,
    CATEGORY_OTHER,
)

#: Reviewed default mapping (product doc 2.1, from configs/knowledge/apis.yaml).
#: 产量与进度 (6) -> output; 工资与排名 (2) -> payroll; 生产计划与制单 (4) -> order;
#: 认证与凭证 (3) + 基础数据 (9) + 吊挂 (3) -> other.
DEFAULT_OPERATION_CATEGORIES: dict[str, str] = {
    # 产量查询（产量与进度 6）
    "BarcodeClQuery": CATEGORY_OUTPUT,
    "HuohaoWtCLQuery": CATEGORY_OUTPUT,
    "PinFengGridPageList": CATEGORY_OUTPUT,
    "WorktypeProgressQuery": CATEGORY_OUTPUT,
    "YskQuery": CATEGORY_OUTPUT,
    "WskQuery": CATEGORY_OUTPUT,
    # 工资查询（工资与排名 2）
    "GongziMxQuery": CATEGORY_PAYROLL,
    "GongziJeOrderQuery": CATEGORY_PAYROLL,
    # 订单进度（生产计划与制单 4）
    "PlanGridPageList": CATEGORY_ORDER,
    "SclzdGridPageList": CATEGORY_ORDER,
    "SclzdWorktypeQuery": CATEGORY_ORDER,
    "SclzdBarcodeQuery": CATEGORY_ORDER,
    # 其他：认证与凭证 (3)
    "SystemToken": CATEGORY_OTHER,
    "QuerySign": CATEGORY_OTHER,
    "TestPermissions": CATEGORY_OTHER,
    # 其他：基础数据 (9)
    "UserInfoQuery": CATEGORY_OTHER,
    "MoveMenuQuery": CATEGORY_OTHER,
    "HuohaoQuery": CATEGORY_OTHER,
    "HuohaoFormQuery": CATEGORY_OTHER,
    "ScTypeQuery": CATEGORY_OTHER,
    "RfidWorktypeQuery": CATEGORY_OTHER,
    "HuohaoWorktypeQuery": CATEGORY_OTHER,
    "EmployeeQuery": CATEGORY_OTHER,
    "DeptQuery": CATEGORY_OTHER,
    # 其他：吊挂 (3)
    "DgGridPageList": CATEGORY_OTHER,
    "DgZuGridPageList": CATEGORY_OTHER,
    "DgClQuery": CATEGORY_OTHER,
}


class MesCategoryResolver:
    """Resolves an ``operation_id`` to its billing category.

    The live mapping comes from the ``mes_operation_category`` table
    (factory-agent owned, read-only here); when the table is not present yet
    or a row is missing, the reviewed default mapping
    applies and unknown operations fall back to ``other``.
    """

    def __init__(
        self,
        store: UsageStore,
        *,
        defaults: dict[str, str] | None = None,
        loader: Callable[[], Awaitable[list[MesOperationCategory]]] | None = None,
    ) -> None:
        self._store = store
        self._defaults = defaults or DEFAULT_OPERATION_CATEGORIES
        self._loader = loader or store.list_mes_operation_categories

    async def category_for(self, operation_id: str) -> str:
        rows = await self._loader()
        for row in rows:
            if row.operation_id == operation_id:
                return row.category
        return self._defaults.get(operation_id, CATEGORY_OTHER)

    async def categories_for(self, operation_ids: frozenset[str]) -> dict[str, str]:
        rows = await self._loader()
        by_operation = {row.operation_id: row.category for row in rows}
        return {
            operation_id: by_operation.get(
                operation_id, self._defaults.get(operation_id, CATEGORY_OTHER)
            )
            for operation_id in operation_ids
        }


__all__ = [
    "CATEGORY_ORDER",
    "CATEGORY_ORDERING",
    "CATEGORY_OTHER",
    "CATEGORY_OUTPUT",
    "CATEGORY_PAYROLL",
    "DEFAULT_OPERATION_CATEGORIES",
    "MesCategoryResolver",
]
