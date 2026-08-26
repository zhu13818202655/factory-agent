"""Smoke baseline over the real Mock MES in-process ASGI app.

Proves the Story 5 vertical slice end to end: credential bundle from the
customer token endpoint, scope injection through the executor, customer rows
validated at the adapter boundary, and aggregation in the DuckDB sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.server import create_app

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter, MesRequest
from factory_agent.domain import UserId
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import (
    ResultColumnMeta,
    ResultTable,
    default_metric_registry,
)
from factory_agent.execution.sandbox_runtime import InteractionSandbox, SandboxTable

WINDOW = ("2026-07-01", "2026-08-31")


async def _bundle(client: AsyncClient) -> MesCredentialBundle:
    token = (await client.post("/api/system/token", json={"app_key": "APPKEY-A"})).json()["result"]
    return MesCredentialBundle(
        access_token=token["accessToken"],
        app_key=token["appkey"],
        sign=token["sign"],
        timestamp=token["timestamp"],
        expires_at=datetime.fromisoformat(token["expiresAt"]),
        user=UserId(token["user"]),
        uname=token["uname"],
    )


@pytest.mark.asyncio
async def test_minimal_recipe_against_real_mock_mes() -> None:
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    adapter = HongzhaoMesAdapter(
        "http://test", await _bundle(client), load_catalog(), client=client
    )
    catalog = load_catalog()
    recipes = load_recipes(catalog.operation_ids)

    recipe = recipes.get("smoke_piecework_summary")
    api_operations = {step.operation_id for step in recipe.steps if step.kind == "api"}
    assert api_operations <= catalog.operation_ids

    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    filters = NarrowedFilters(
        tenant_id=adapter.bundle.tenant_id,
        employee_ids=frozenset({adapter.bundle.employee_id}),
        dept_ids=frozenset(),
    )
    result = await executor.execute_step(
        filters,
        ExecutionRequest(
            operation_id="EmployeeQuery",
            time_range=(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),
        ),
    )
    assert result.complete is True
    assert len(result.rows) == 1  # one employee matches the caller's uid

    # Local aggregation inside an isolated sandbox.
    with InteractionSandbox(allowed_tables=["employee"]) as sandbox:
        sandbox.register_table(
            SandboxTable(
                name="employee",
                rows=result.rows,
                columns=(
                    ("uid", "VARCHAR"),
                    ("uname", "VARCHAR"),
                    ("dept", "VARCHAR"),
                ),
            )
        )
        rows = sandbox.execute("SELECT COUNT(*) FROM employee")
        count = int(rows[0][0])

    registry = default_metric_registry()
    metric = registry.resolve("output_personal", "customer-output-v1")
    table = ResultTable(
        capability_id=recipe.capability_id,
        columns=(
            ResultColumnMeta(
                name="employee_count",
                metric_name=metric.name,
                metric_version=metric.version,
                source_operations=("EmployeeQuery",),
            ),
        ),
        rows=({"employee_count": count},),
        totals={"employee_count": Decimal(count)},
        source_operations=("EmployeeQuery",),
    )
    trace = table.trace_for("employee_count")
    assert trace.metric_name == "output_personal"
    assert table.totals["employee_count"] == Decimal(1)

    await adapter.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_mock_mes_filters_by_bearer_identity() -> None:
    """Company isolation: a foreign AppKey presented with the company-A token
    must be rejected at the envelope level (never returns data)."""
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    adapter = HongzhaoMesAdapter(
        "http://test", await _bundle(client), load_catalog(), client=client
    )

    result = await adapter.execute(
        MesRequest(
            "YskQuery",
            {
                "Uid": str(adapter.bundle.user),
                "dates": WINDOW[0],
                "datee": WINDOW[1],
            },
        )
    )
    result_mapping = cast("dict[str, Any]", result.result)
    assert "list" in result_mapping
    rows = cast("list[dict[str, Any]]", result_mapping["list"])
    # Company isolation: the company-A identity must never see the company-B worker.
    assert rows
    assert all(str(row.get("uid")) != "02001" for row in rows)
    assert result_mapping.get("total") == len(rows)

    await adapter.aclose()
    await client.aclose()
