from __future__ import annotations

import pytest

from factory_agent.domain import InteractionId, TenantId
from factory_agent.ports import ModelGatewayError, ModelRequest, ModelStage, SessionRecord
from factory_agent.ports.not_configured import (
    DependencyNotConfiguredError,
    NotConfiguredArtifactStore,
    NotConfiguredCacheStore,
    NotConfiguredClock,
    NotConfiguredIdentityProvider,
    NotConfiguredMesDataSource,
    NotConfiguredModelGateway,
    NotConfiguredSessionRepository,
)
from tests.support.ports import FakeArtifactStore, FakeSessionRepository


@pytest.mark.asyncio
async def test_not_configured_dependencies_fail_explicitly() -> None:
    with pytest.raises(DependencyNotConfiguredError):
        await NotConfiguredIdentityProvider().authenticate("credential")
    with pytest.raises(DependencyNotConfiguredError):
        await NotConfiguredMesDataSource().execute(object())
    with pytest.raises(ModelGatewayError):
        await NotConfiguredModelGateway().complete(
            ModelRequest(
                model_alias="factory-fast",
                messages=(),
                stage=ModelStage.CLASSIFY,
                logical_call_id="call-1",
            )
        )
    with pytest.raises(DependencyNotConfiguredError):
        await NotConfiguredSessionRepository().get(InteractionId("interaction-1"))
    with pytest.raises(DependencyNotConfiguredError):
        await NotConfiguredArtifactStore().get("artifact-1")
    with pytest.raises(DependencyNotConfiguredError):
        NotConfiguredClock().now()
    with pytest.raises(DependencyNotConfiguredError):
        await NotConfiguredCacheStore().get("cache-key")


@pytest.mark.asyncio
async def test_fake_session_repository_copies_records() -> None:
    repository = FakeSessionRepository()
    record = SessionRecord(
        interaction_id=InteractionId("interaction-1"),
        tenant_id=TenantId("tenant-1"),
        payload=b"state",
    )
    await repository.put(record)

    assert await repository.get(record.interaction_id) == record


@pytest.mark.asyncio
async def test_fake_artifact_store_supports_full_lifecycle() -> None:
    store = FakeArtifactStore()
    await store.put("artifact-1", b"content", "application/octet-stream")

    assert await store.get("artifact-1") == b"content"
    assert (
        await store.presign("artifact-1", 60) == "https://artifacts.invalid/artifact-1?expires=60"
    )
    await store.delete("artifact-1")
    with pytest.raises(KeyError):
        await store.get("artifact-1")
