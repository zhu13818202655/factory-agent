from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import factory_agent.bootstrap as bootstrap
from factory_agent.application.cache import AuthAwareCache
from factory_agent.bootstrap import DependencyOverrides, build_container
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter, MesRequest
from factory_agent.data_api.pagination import PagerBudget
from factory_agent.domain import UserId
from factory_agent.ports import AuthenticatedIdentity
from tests.support.cache import InMemoryCacheStore
from tests.support.ports import FakeIdentityProvider, FakeMesDataSource


def test_offline_container_starts_with_explicit_dependency_status() -> None:
    container = build_container(FactoryAgentSettings())

    assert container.capabilities.capabilities == ()
    assert container.readiness["mes"] == "not_configured"
    assert container.readiness["postgres"] == "not_configured"
    assert container.readiness["litellm"] == "not_configured"
    assert container.readiness["redis"] == "not_configured"


def test_container_accepts_test_fakes_without_external_configuration() -> None:
    identity = FakeIdentityProvider(AuthenticatedIdentity(subject_id="subject-1"))
    mes = FakeMesDataSource(response={"items": []})
    container = build_container(
        FactoryAgentSettings(),
        DependencyOverrides(identity=identity, mes=mes),
    )

    assert container.identity is identity
    assert container.mes is mes
    assert container.readiness["identity"] == "fake"
    assert container.readiness["mes"] == "fake"


def test_canonical_adapter_is_selected_only_when_configured() -> None:
    settings = FactoryAgentSettings.model_validate(
        {"canonical_mes_base_url": "http://mes-gateway:9002"}
    )

    container = build_container(settings)

    assert container.readiness["mes"] == "configured"


def test_export_readiness_names_the_active_artifact_backend() -> None:
    from factory_agent.export_service import ExportService

    local = build_container(FactoryAgentSettings())
    assert local.readiness["export"] == "local"
    assert isinstance(local.artifact_exporter, ExportService)

    s3 = build_container(
        FactoryAgentSettings.model_validate({"s3_endpoint_url": "http://seaweedfs:8333"})
    )
    assert s3.readiness["export"] == "s3"

    # An all-whitespace endpoint must not silently enable an unreachable
    # gateway: it degrades to the local directory backend.
    blank = build_container(FactoryAgentSettings.model_validate({"s3_endpoint_url": "   "}))
    assert blank.readiness["export"] == "local"


def _bundle() -> MesCredentialBundle:
    return MesCredentialBundle(
        access_token="unconfigured",
        app_key="unconfigured",
        sign="unconfigured",
        timestamp=0,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("unconfigured"),
        uname="unconfigured",
    )


@pytest.mark.asyncio
async def test_hongzhao_adapter_rejects_unreviewed_operations_before_http() -> None:
    from factory_agent.domain.errors import UnsupportedOperationError

    adapter = HongzhaoMesAdapter("http://mes-gateway:9002", _bundle(), load_catalog())

    with pytest.raises(UnsupportedOperationError):
        await adapter.execute(MesRequest(operation_id="unreviewed", params={}))


def test_owner_snapshot_budget_is_independent_of_interactive_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interactive: list[PagerBudget] = []
    snapshot: list[PagerBudget] = []

    class RecordingAdapter(HongzhaoMesAdapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            budget = kwargs["pager_budget"]
            assert isinstance(budget, PagerBudget)
            interactive.append(budget)
            super().__init__(*args, **kwargs)

    def owner_pool(*args: Any, **kwargs: Any) -> Any:
        budget = kwargs["pager_budget"]
        assert isinstance(budget, PagerBudget)
        snapshot.append(budget)
        return SimpleNamespace(fetchers=())

    def memory_cache(settings: FactoryAgentSettings) -> AuthAwareCache:
        return AuthAwareCache(
            InMemoryCacheStore(),
            contract_version="fixture",
            metric_version="fixture",
            data_version="fixture",
        )

    monkeypatch.setattr(bootstrap, "HongzhaoMesAdapter", RecordingAdapter)
    monkeypatch.setattr(bootstrap, "OwnerSnapshotPool", owner_pool)
    monkeypatch.setattr(bootstrap, "_build_cache", memory_cache)
    build_container(
        FactoryAgentSettings.model_validate(
            {
                "canonical_mes_base_url": "http://mock.invalid",
                "mes_max_rows": 7,
                "result_cache_snapshot_max_rows": 42,
            }
        )
    )
    assert len(interactive) == len(snapshot) == 1
    assert interactive[0].max_rows == 7
    assert snapshot[0].max_rows == 42
    assert interactive[0].max_pages == snapshot[0].max_pages == 20
