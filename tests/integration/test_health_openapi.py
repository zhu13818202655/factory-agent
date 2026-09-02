from __future__ import annotations

from fastapi import FastAPI

from factory_agent.api.server import create_app as create_factory_app


def assert_liveness_contract(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    for path in ("/health/live", "/health/ready"):
        operation = paths[path]["get"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]


def test_factory_agent_publishes_liveness_contract() -> None:
    assert_liveness_contract(create_factory_app())


def test_mock_mes_publishes_liveness_contract(mock_mes_database_url: str) -> None:
    # The mock is PG-backed; its published health surface still applies.
    from mock_mes.testing import make_test_app

    assert_liveness_contract(make_test_app(mock_mes_database_url))
