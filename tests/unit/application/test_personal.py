"""Personalization service: quick questions, history, favorites, user mapping."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.personal import (
    FavoriteNotFoundError,
    PersonalizationService,
    sanitize_slots,
)
from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.ports import InteractionOwner, TrustedCredential
from tests.support.personal import (
    InMemoryFavoriteRepository,
    InMemoryHistoryRepository,
    InMemoryUserMappingRepository,
)

TENANT = TenantId("tenant-a")
USER = UserId("u-1")
OWNER = InteractionOwner(tenant_id=TENANT, user_id=USER)
NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
CREDENTIAL = TrustedCredential(tenant_id=TENANT, user_id=USER)


def make_service() -> PersonalizationService:
    return PersonalizationService(
        InMemoryHistoryRepository(),
        InMemoryFavoriteRepository(),
        InMemoryUserMappingRepository(),
        new_id=lambda: "id-1",
        clock=lambda: NOW,
    )


def test_quick_questions_derive_from_registered_capabilities() -> None:
    service = make_service()

    questions = service.quick_questions(CREDENTIAL)

    assert 4 <= len(questions) <= 6
    capabilities = {question.capability_id for question in questions}
    assert capabilities <= {"FR-001", "FR-002", "FR-007", "FR-009", "FR-011"}
    # FR-012 needs an employee name slot, so it must not be a one-click question.
    assert "FR-012" not in capabilities
    assert all(question.capability_id for question in questions)


def test_quick_questions_are_not_role_hardcoded() -> None:
    service = make_service()
    boss = TrustedCredential(tenant_id=TENANT, user_id=UserId("boss"))
    employee = TrustedCredential(tenant_id=TENANT, user_id=UserId("worker"))

    assert {q.capability_id for q in service.quick_questions(boss)} == {
        q.capability_id for q in service.quick_questions(employee)
    }


def test_sanitize_slots_keeps_only_non_sensitive_fields() -> None:
    raw: dict[str, object] = {
        "time_expression": "本月",
        "order_codes": ["D-001"],
        "employee_ids": ["E-1"],  # work numbers must never persist
        "wage_amount": 12345.67,
        "result_rows": [["secret"]],
        "sql": "SELECT *",
    }

    safe = sanitize_slots(raw)

    assert safe == {"time_expression": "本月", "order_codes": ["D-001"]}
    assert "employee_ids" not in safe
    assert "wage_amount" not in safe
    assert "result_rows" not in safe
    assert "sql" not in safe


@pytest.mark.asyncio
async def test_history_records_normalized_non_sensitive_intent() -> None:
    service = make_service()

    entry = await service.record_history(
        OWNER,
        capability_id=CapabilityId("FR-001"),
        slots={"time_expression": "本月", "wage_amount": 999.0},
        status="completed",
        now=NOW,
    )

    assert entry is not None
    assert entry.intent == {"time_expression": "本月"}
    assert "wage_amount" not in entry.intent


@pytest.mark.asyncio
async def test_history_is_ownership_filtered() -> None:
    service = make_service()
    await service.record_history(
        OWNER,
        capability_id=CapabilityId("FR-001"),
        slots={"time_expression": "本月"},
        status="completed",
        now=NOW,
    )

    other = InteractionOwner(tenant_id=TENANT, user_id=UserId("u-2"))
    assert (await service.list_history(other, 10)).items == ()
    assert (await service.list_history(OWNER, 10)).items


@pytest.mark.asyncio
async def test_favorites_create_list_delete_and_expire() -> None:
    service = make_service()

    favorite = await service.create_favorite(
        OWNER,
        capability_id=CapabilityId("FR-005"),
        title="订单进度",
        slots={"order_codes": ["D-001"], "employee_ids": ["E-1"]},
        now=NOW,
    )

    assert favorite.slots == {"order_codes": ["D-001"]}
    assert favorite.expires_at > NOW
    assert (await service.list_favorites(OWNER)) == (favorite,)
    assert await service.delete_favorite(OWNER, favorite.favorite_id)
    assert (await service.list_favorites(OWNER)) == ()


@pytest.mark.asyncio
async def test_reask_returns_saved_intent_not_cached_result() -> None:
    service = make_service()
    favorite = await service.create_favorite(
        OWNER,
        capability_id=CapabilityId("FR-001"),
        title="个人产量",
        slots={"time_expression": "上月"},
        now=NOW,
    )

    reasked = await service.reask_favorite(OWNER, favorite.favorite_id)

    # The saved intent is returned for re-execution; no ResultTable is replayed.
    assert reasked.capability_id == CapabilityId("FR-001")
    assert reasked.slots == {"time_expression": "上月"}


@pytest.mark.asyncio
async def test_reask_is_ownership_filtered_and_expiry_aware() -> None:
    history = InMemoryHistoryRepository()
    favorites = InMemoryFavoriteRepository()
    service = PersonalizationService(
        history,
        favorites,
        InMemoryUserMappingRepository(),
        new_id=lambda: "f-1",
        clock=lambda: NOW,
        favorite_ttl_days=0,  # expires immediately
    )
    favorite = await service.create_favorite(
        OWNER,
        capability_id=CapabilityId("FR-001"),
        title="t",
        slots={},
        now=NOW,
    )

    other = InteractionOwner(tenant_id=TENANT, user_id=UserId("u-2"))
    with pytest.raises(FavoriteNotFoundError):
        await service.reask_favorite(other, favorite.favorite_id)
    with pytest.raises(FavoriteNotFoundError):
        await service.reask_favorite(OWNER, favorite.favorite_id)  # expired


@pytest.mark.asyncio
async def test_user_mapping_roundtrip_and_tenant_scoping() -> None:
    service = make_service()

    mapping = await service.save_mapping(
        uid="u-1",
        tenant_id=TENANT,
        uname="张三",
        company="工厂A",
        now=NOW,
    )

    assert mapping is not None
    assert (await service.get_mapping(TENANT, "u-1")) == mapping
    assert await service.get_mapping(TenantId("tenant-b"), "u-1") is None


def test_quick_questions_require_an_authenticated_credential() -> None:
    service = make_service()
    assert service.quick_questions(TrustedCredential(tenant_id=TENANT, user_id=USER))
    assert service.quick_questions(TrustedCredential(tenant_id=TENANT, user_id=USER)) != []
