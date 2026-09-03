"""Quick questions, query history, favorites, and the minimal user mapping.

History and favorites store only normalized, non-sensitive slots — never raw
question text, work numbers, wage/output amounts, or old ``DataScope`` IDs.
Re-asking a favorite never replays a cached result: the client re-issues the
intent and the session pipeline re-resolves credentials, scope, metric version,
and current data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from factory_agent.application.permission_matrix import (
    REGISTERED_CAPABILITIES,
    capabilities_for_role,
)
from factory_agent.domain import CapabilityId, Role, TenantId
from factory_agent.ports import InteractionOwner, TrustedCredential
from factory_agent.ports.personal import (
    Favorite,
    FavoriteRepository,
    HistoryEntry,
    HistoryPage,
    HistoryRepository,
    UserMapping,
    UserMappingRepository,
)

#: Slots that are safe to persist; everything else (work numbers, wage values,
#: scope IDs, result rows) is rejected.
_NON_SENSITIVE_SLOTS: frozenset[str] = frozenset(
    {
        "time_expression",
        "time_range_start",
        "time_range_end",
        "order_codes",
        "plan_codes",
        "style_codes",
        "dept_names",
        "employee_names",
    }
)

#: Favorites are short-lived by default so stale intents never linger.
DEFAULT_FAVORITE_TTL_DAYS = 90


@dataclass(frozen=True, slots=True)
class QuickQuestion:
    id: str
    capability_id: str
    text: str
    slots: dict[str, object]


#: Reviewed role-aware quick questions (角色化快捷问题，见
#: ``docs/product/需求及方案整理.md`` 通用功能表). Each role sees 4–6 common
#: phrasings drawn from the capabilities its matrix allows; capabilities that
#: need an extra mandatory slot (e.g. FR-012 employee names) are intentionally
#: not one-click quick questions.
_QUICK_QUESTIONS: tuple[tuple[Role, QuickQuestion], ...] = (
    # 员工（本人维度，四角色通用）
    (
        Role.EMPLOYEE,
        QuickQuestion(
            "qq-own-output", "FR-001", "我这个月的个人产量是多少？", {"time_expression": "本月"}
        ),
    ),
    (
        Role.EMPLOYEE,
        QuickQuestion("qq-own-wage", "FR-002", "我这个月的工资汇总", {"time_expression": "本月"}),
    ),
    (
        Role.EMPLOYEE,
        QuickQuestion(
            "qq-own-wage-detail",
            "FR-003",
            "我这个月的工资明细是怎么算的？",
            {"time_expression": "本月"},
        ),
    ),
    (
        Role.EMPLOYEE,
        QuickQuestion("qq-own-rank", "FR-004", "我在小组里排第几？", {"time_expression": "本月"}),
    ),
    # 组长（本人 + 管理面）
    (
        Role.GROUP_LEADER,
        QuickQuestion(
            "qq-leader-own-wage", "FR-002", "我这个月的工资汇总", {"time_expression": "本月"}
        ),
    ),
    (
        Role.GROUP_LEADER,
        QuickQuestion(
            "qq-leader-order-progress",
            "FR-005",
            "这个订单现在做到哪道工序了？",
            {"time_expression": "本月"},
        ),
    ),
    (
        Role.GROUP_LEADER,
        QuickQuestion(
            "qq-leader-order-output",
            "FR-006",
            "这个款这周做了多少产量？",
            {"time_expression": "本周"},
        ),
    ),
    (
        Role.GROUP_LEADER,
        QuickQuestion(
            "qq-leader-team-payroll",
            "FR-008",
            "这个月我们组每人工资清单",
            {"time_expression": "本月"},
        ),
    ),
    # 管理（绑定车间/部门）
    (
        Role.MANAGER,
        QuickQuestion(
            "qq-manager-order-progress",
            "FR-005",
            "这个订单现在做到哪道工序了？",
            {"time_expression": "本月"},
        ),
    ),
    (
        Role.MANAGER,
        QuickQuestion(
            "qq-manager-order-output",
            "FR-006",
            "这个款这周做了多少产量？",
            {"time_expression": "本周"},
        ),
    ),
    (
        Role.MANAGER,
        QuickQuestion(
            "qq-manager-workshop-compare",
            "FR-007",
            "各小组这个月产量对比一下",
            {"time_expression": "本月"},
        ),
    ),
    (
        Role.MANAGER,
        QuickQuestion(
            "qq-manager-team-payroll",
            "FR-008",
            "这个月我们车间每人工资清单",
            {"time_expression": "本月"},
        ),
    ),
    # 老板（全厂）
    (
        Role.OWNER,
        QuickQuestion(
            "qq-owner-orders", "FR-009", "所有订单现在进度怎么样？", {"time_expression": "本月"}
        ),
    ),
    (
        Role.OWNER,
        QuickQuestion(
            "qq-owner-workshop-output",
            "FR-010",
            "整个车间这个月产量情况",
            {"time_expression": "本月"},
        ),
    ),
    (
        Role.OWNER,
        QuickQuestion(
            "qq-owner-payroll", "FR-011", "这个月整个厂工资发多少？", {"time_expression": "本月"}
        ),
    ),
    (
        Role.OWNER,
        QuickQuestion(
            "qq-owner-own-wage", "FR-002", "我这个月的工资汇总", {"time_expression": "本月"}
        ),
    ),
)


class FavoriteNotFoundError(LookupError):
    """Missing favorite or one owned by another user."""


def sanitize_slots(slots: dict[str, object]) -> dict[str, object]:
    """Keep only the whitelisted non-sensitive slot names."""
    return {key: value for key, value in slots.items() if key in _NON_SENSITIVE_SLOTS}


class PersonalizationService:
    def __init__(
        self,
        history: HistoryRepository | None = None,
        favorites: FavoriteRepository | None = None,
        users: UserMappingRepository | None = None,
        *,
        new_id: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        favorite_ttl_days: int = DEFAULT_FAVORITE_TTL_DAYS,
    ) -> None:
        self._history = history
        self._favorites = favorites
        self._users = users
        self._new_id = new_id or (lambda: "id")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._favorite_ttl_days = favorite_ttl_days

    # -- Quick questions -----------------------------------------------------
    def quick_questions(
        self, credential: TrustedCredential, role: Role | None = None
    ) -> list[QuickQuestion]:
        """Role-aware quick questions available to this credential.

        Availability is the reviewed capability-role matrix intersected with
        the registered capability registry. Without a role (degraded fixtures)
        no questions are returned — a roleless list would violate the
        role-aware presentation contract.
        """
        if not credential.tenant_id or not credential.user_id or role is None:
            return []
        registered = {capability.value for capability in REGISTERED_CAPABILITIES}
        allowed = {capability.value for capability in capabilities_for_role(role)}
        return [
            question
            for question_role, question in _QUICK_QUESTIONS
            if question_role is role
            and question.capability_id in registered
            and question.capability_id in allowed
        ]

    # -- History -------------------------------------------------------------
    async def record_history(
        self,
        owner: InteractionOwner,
        *,
        capability_id: CapabilityId,
        slots: dict[str, object],
        status: str,
        now: datetime,
    ) -> HistoryEntry | None:
        if self._history is None:
            return None
        entry = HistoryEntry(
            history_id=self._new_id(),
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            capability_id=capability_id,
            intent=sanitize_slots(slots),
            status=status,
            created_at=now,
        )
        await self._history.record(entry)
        return entry

    async def list_history(
        self,
        owner: InteractionOwner,
        limit: int,
        cursor: str | None = None,
    ) -> HistoryPage:
        if self._history is None:
            return HistoryPage((), None)
        return await self._history.list(owner.tenant_id, owner.user_id, limit, cursor)

    async def delete_history(self, owner: InteractionOwner, history_id: str) -> bool:
        if self._history is None:
            return False
        return await self._history.delete(owner.tenant_id, owner.user_id, history_id)

    # -- Favorites -----------------------------------------------------------
    async def create_favorite(
        self,
        owner: InteractionOwner,
        *,
        capability_id: CapabilityId,
        title: str,
        slots: dict[str, object],
        now: datetime,
    ) -> Favorite:
        if self._favorites is None:
            raise FavoriteNotFoundError("favorites are not configured")
        safe_slots = sanitize_slots(slots)
        favorite = Favorite(
            favorite_id=self._new_id(),
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            capability_id=capability_id,
            title=title,
            slots=safe_slots,
            created_at=now,
            expires_at=now + timedelta(days=self._favorite_ttl_days),
        )
        await self._favorites.save(favorite)
        return favorite

    async def list_favorites(
        self, owner: InteractionOwner, limit: int = 50
    ) -> tuple[Favorite, ...]:
        if self._favorites is None:
            return ()
        return await self._favorites.list(owner.tenant_id, owner.user_id, limit)

    async def delete_favorite(self, owner: InteractionOwner, favorite_id: str) -> bool:
        if self._favorites is None:
            return False
        return await self._favorites.delete(owner.tenant_id, owner.user_id, favorite_id)

    async def reask_favorite(self, owner: InteractionOwner, favorite_id: str) -> Favorite:
        """Return the saved intent for a one-click re-ask.

        The caller re-issues this intent through the session pipeline, which
        re-resolves credentials, scope, metric version, and current data. No
        cached result is ever replayed.
        """
        if self._favorites is None:
            raise FavoriteNotFoundError("favorites are not configured")
        favorite = await self._favorites.get(owner.tenant_id, owner.user_id, favorite_id)
        if favorite is None:
            raise FavoriteNotFoundError("favorite not found")
        if self._clock() >= favorite.expires_at:
            raise FavoriteNotFoundError("favorite has expired")
        return favorite

    # -- Minimal user mapping (uid ↔ uname display convenience) -------------
    async def save_mapping(
        self,
        *,
        uid: str,
        tenant_id: TenantId,
        uname: str,
        company: str | None,
        now: datetime,
    ) -> UserMapping | None:
        if self._users is None:
            return None
        mapping = UserMapping(
            uid=uid,
            tenant_id=tenant_id,
            uname=uname,
            company=company,
            updated_at=now,
        )
        await self._users.upsert(mapping)
        return mapping

    async def get_mapping(self, tenant_id: TenantId, uid: str) -> UserMapping | None:
        if self._users is None:
            return None
        return await self._users.get(tenant_id, uid)


__all__ = [
    "DEFAULT_FAVORITE_TTL_DAYS",
    "FavoriteNotFoundError",
    "PersonalizationService",
    "QuickQuestion",
    "sanitize_slots",
]
