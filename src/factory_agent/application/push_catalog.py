"""Push subscription content-item catalog (Story 3B).

Reviewed data: each content item maps to one L1 capability whose capability-role
matrix (``permission_matrix.CAPABILITY_ROLES``) is the role ceiling for the item
(推送项按角色数据范围展示). The item → FR mapping below follows the product
「通用功能」推送项 list (``docs/product/需求及方案整理.md``); final FR mapping
for ambiguous items is re-validated in the customer dry-runs (Story 3 双跑).

The morning report (每日早报) is default-on and NOT configurable off; it is
composed per role from the personal/management/owner summary capabilities below.
"""

from __future__ import annotations

from dataclasses import dataclass

from factory_agent.application.permission_matrix import CAPABILITY_ROLES, Capability, Role


@dataclass(frozen=True, slots=True)
class ContentItem:
    """One selectable weekly/monthly push item."""

    item_id: str
    title: str
    capability: Capability
    roles: frozenset[Role]

    def available_for(self, role: Role) -> bool:
        return role in self.roles


#: Fixed, reviewed subscription content items (产品「通用功能」推送项).
CONTENT_ITEMS: tuple[ContentItem, ...] = (
    ContentItem(
        "wage_detail_push",
        "工资明细推送",
        Capability.OWN_PAYROLL_DETAIL,
        CAPABILITY_ROLES[Capability.OWN_PAYROLL_DETAIL],
    ),
    ContentItem(
        "order_progress_summary",
        "订单进度汇总",
        Capability.ORDER_PROGRESS,
        CAPABILITY_ROLES[Capability.ORDER_PROGRESS],
    ),
    ContentItem(
        "style_output_ranking",
        "货号产量排名",
        Capability.ORDER_OUTPUT,
        CAPABILITY_ROLES[Capability.ORDER_OUTPUT],
    ),
    ContentItem(
        "completion_overview",
        "完工进度总览",
        Capability.FACTORY_ORDER_OVERVIEW,
        CAPABILITY_ROLES[Capability.FACTORY_ORDER_OVERVIEW],
    ),
    ContentItem(
        "production_completion",
        "生产完工进度",
        Capability.WORKSHOP_OUTPUT_OVERVIEW,
        CAPABILITY_ROLES[Capability.WORKSHOP_OUTPUT_OVERVIEW],
    ),
    ContentItem(
        "weekly_output_summary",
        "本周产量汇总",
        Capability.FACTORY_PAYROLL_STATS,
        CAPABILITY_ROLES[Capability.FACTORY_PAYROLL_STATS],
    ),
)

_BY_ID: dict[str, ContentItem] = {item.item_id: item for item in CONTENT_ITEMS}


def content_item(item_id: str) -> ContentItem | None:
    return _BY_ID.get(item_id)


def content_items_for_role(role: Role) -> tuple[ContentItem, ...]:
    """Items a role may subscribe to (role data-range ceiling)."""
    return tuple(item for item in CONTENT_ITEMS if item.available_for(role))


def content_item_ids_for_role(role: Role) -> frozenset[str]:
    return frozenset(item.item_id for item in content_items_for_role(role))


#: Reviewed mapping used to compose the default daily morning report: which
#: capability produces "昨日产量/工资摘要" for each role. 00 → 本人产量+工资；
#: 01/02 → 本人 + 绑定车间/部门生产与工资；99 → 全厂摘要。
MORNING_REPORT_CAPABILITIES: dict[Role, tuple[Capability, ...]] = {
    Role.EMPLOYEE: (Capability.OWN_OUTPUT, Capability.OWN_PAYROLL_SUMMARY),
    Role.GROUP_LEADER: (Capability.OWN_OUTPUT, Capability.WORKSHOP_COMPARISON),
    Role.MANAGER: (Capability.OWN_OUTPUT, Capability.WORKSHOP_COMPARISON),
    Role.OWNER: (Capability.WORKSHOP_OUTPUT_OVERVIEW, Capability.FACTORY_PAYROLL_STATS),
}


__all__ = [
    "CONTENT_ITEMS",
    "ContentItem",
    "MORNING_REPORT_CAPABILITIES",
    "content_item",
    "content_item_ids_for_role",
    "content_items_for_role",
]
