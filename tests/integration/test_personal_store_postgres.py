"""PostgreSQL integration tests for history, favorites, and user mapping.

Requires ``FACTORY_AGENT_TEST_POSTGRES_URL`` (like the session store tests);
skipped otherwise. Proves ownership filtering and the migration are durable,
not just in-memory behavior.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.personal_store import (
    SqlFavoriteRepository,
    SqlHistoryRepository,
    SqlUserMappingRepository,
)
from factory_agent.ports.personal import (
    Favorite,
    HistoryEntry,
    UserMapping,
)

DATABASE_URL = os.environ.get("FACTORY_AGENT_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="set FACTORY_AGENT_TEST_POSTGRES_URL to a disposable database to run these tests",
)

TENANT = TenantId("tenant-a")
USER = UserId("u-1")
OTHER = UserId("u-2")
NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


def make_repos():
    engine = create_session_engine(str(DATABASE_URL))
    return (
        engine,
        SqlHistoryRepository(engine),
        SqlFavoriteRepository(engine),
        SqlUserMappingRepository(engine),
    )


@pytest.mark.asyncio
async def test_history_is_ownership_filtered_in_postgres() -> None:
    engine, history, _favorites, _users = make_repos()
    try:
        await history.record(
            HistoryEntry(
                history_id="h-1",
                tenant_id=TENANT,
                user_id=USER,
                capability_id=CapabilityId("FR-001"),
                intent={"time_expression": "本月"},
                status="completed",
                created_at=NOW,
            )
        )

        mine = await history.list(TENANT, USER, 10)
        other = await history.list(TENANT, OTHER, 10)
        assert len(mine.items) == 1
        assert other.items == ()
        assert mine.items[0].intent == {"time_expression": "本月"}

        assert await history.delete(TENANT, USER, "h-1") is True
        assert await history.delete(TENANT, OTHER, "h-1") is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_favorite_save_get_delete_roundtrip_in_postgres() -> None:
    engine, _history, favorites, _users = make_repos()
    try:
        favorite = Favorite(
            favorite_id="f-1",
            tenant_id=TENANT,
            user_id=USER,
            capability_id=CapabilityId("FR-005"),
            title="订单进度",
            slots={"order_codes": ["D-001"]},
            created_at=NOW,
            expires_at=NOW,
        )
        await favorites.save(favorite)

        assert (await favorites.get(TENANT, USER, "f-1")) == favorite
        assert await favorites.get(TENANT, OTHER, "f-1") is None
        assert (await favorites.list(TENANT, USER, 10)) == (favorite,)
        assert await favorites.delete(TENANT, USER, "f-1") is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_mapping_upsert_and_get_in_postgres() -> None:
    engine, _history, _favorites, users = make_repos()
    try:
        mapping = UserMapping(
            uid="u-1",
            tenant_id=TENANT,
            uname="张三",
            company="工厂A",
            updated_at=NOW,
        )
        await users.upsert(mapping)
        await users.upsert(
            UserMapping(uid="u-1", tenant_id=TENANT, uname="张三改", company=None, updated_at=NOW)
        )

        loaded = await users.get(TENANT, "u-1")
        assert loaded is not None
        assert loaded.uname == "张三改"
        assert await users.get(TenantId("tenant-b"), "u-1") is None
    finally:
        await engine.dispose()
