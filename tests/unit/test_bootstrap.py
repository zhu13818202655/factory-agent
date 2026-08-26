from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory_agent.bootstrap import DependencyOverrides, build_container
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter, MesRequest
from factory_agent.domain import UserId
from factory_agent.ports import AuthenticatedIdentity
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
        {"canonical_mes_base_url": "http://mock-mes:8010"}
    )

    container = build_container(settings)

    assert container.readiness["mes"] == "configured"


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

    adapter = HongzhaoMesAdapter("http://mock-mes:8010", _bundle(), load_catalog())

    with pytest.raises(UnsupportedOperationError):
        await adapter.execute(MesRequest(operation_id="unreviewed", params={}))
