"""Smoke baseline over the real Mock MES in-process ASGI app.

Proves one minimal recipe (single API + local aggregation) end to end through
the scoped executor and the DuckDB sandbox, as the Story 5 vertical-slice
baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.server import create_app

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.canonical import (
    CanonicalMesAdapter,
    FetchingAdapter,
    fetch_resource_rows,
)
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    ScopeVersion,
    TenantId,
    TimeRange,
)
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import (
    ResultColumnMeta,
    ResultTable,
    default_metric_registry,
)
from factory_agent.execution.sandbox_runtime import InteractionSandbox, SandboxTable


def _scope() -> DataScope:
    return DataScope(
        tenant_id=TenantId("tenant-a"),
        employee_ids=frozenset({EmployeeId("employee-a1")}),
        dept_ids=frozenset({DeptId("group-a1")}),
        evaluated_at=datetime(2026, 8, 21, tzinfo=UTC),
        scope_version=ScopeVersion("scope-smoke"),
    )


def _filters() -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("tenant-a"),
        employee_ids=frozenset({EmployeeId("employee-a1")}),
        dept_ids=frozenset({DeptId("group-a1")}),
    )


@pytest.mark.asyncio
async def test_minimal_recipe_against_real_mock_mes() -> None:
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    adapter = FetchingAdapter("http://test", "tenant-a-user", client=client)
    catalog = load_catalog()
    recipes = load_recipes(catalog.operation_ids)

    # Recipe must be registered and reference a real catalog operation.
    recipe = recipes.get("smoke_piecework_summary")
    api_steps = [step for step in recipe.steps if step.kind == "api"]
    assert len(api_steps) == 1
    assert api_steps[0].operation_id in catalog

    executor = ScopedExecutor(adapter=adapter, catalog=catalog)  # type: ignore[arg-type]
    result = await executor.execute_step(
        _filters(),
        ExecutionRequest(
            operation_id="C1_listPieceworkRecords",
            time_range=TimeRange(
                datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
            ),
        ),
        active_scope=_scope(),
    )
    assert result.complete is True
    assert len(result.rows) == 1  # employee-a1 has exactly one August record

    # Local aggregation inside an isolated sandbox.
    with InteractionSandbox(allowed_tables=["piecework"]) as sandbox:
        sandbox.register_table(
            SandboxTable(
                name="piecework",
                rows=result.rows,
                columns=(
                    ("record_id", "VARCHAR"),
                    ("qualified_quantity", "DECIMAL(18,4)"),
                    ("amount", "DECIMAL(18,4)"),
                ),
            )
        )
        rows = sandbox.execute("SELECT SUM(qualified_quantity), SUM(amount) FROM piecework")
        quantity_total = Decimal(rows[0][0])
        amount_total = Decimal(rows[0][1])

    registry = default_metric_registry()
    metric = registry.resolve("piecework_wage", "mock-wage-v1")

    table = ResultTable(
        capability_id=recipe.capability_id,
        columns=(
            ResultColumnMeta(
                name="qualified_quantity_total",
                metric_name="output_quantity",
                metric_version="mock-quantity-v1",
                source_operations=("C1_listPieceworkRecords",),
            ),
            ResultColumnMeta(
                name="amount_total",
                metric_name=metric.name,
                metric_version=metric.version,
                source_operations=("C1_listPieceworkRecords",),
            ),
        ),
        rows=(
            {
                "qualified_quantity_total": quantity_total,
                "amount_total": amount_total,
            },
        ),
        totals={
            "qualified_quantity_total": quantity_total,
            "amount_total": amount_total,
        },
        source_operations=("C1_listPieceworkRecords",),
    )

    trace = table.trace_for("amount_total")
    assert trace.metric_name == "piecework_wage"
    # Mock seed: piece-aug qualified 5 at 1.2500 => amount 7.5000? No: 5 * 1.25 = 6.25,
    # but the seeded amount field is authoritative: 7.5000.
    assert table.totals["amount_total"] == Decimal("7.5000")
    assert table.totals["qualified_quantity_total"] == Decimal("5")

    await adapter.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_mock_mes_rejects_out_of_scope_authorized_ids() -> None:
    """Mock enforces scope server-side too; defense in depth holds."""
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    adapter = CanonicalMesAdapter("http://test", "tenant-a-user", client=client)
    from factory_agent.domain.errors import ForbiddenError
    from factory_agent.domain.queries import ResourceQuery

    query = ResourceQuery(
        tenant_id=TenantId("tenant-a"),
        employee_ids=frozenset({EmployeeId("employee-b1")}),  # other tenant's employee
        dept_ids=frozenset({DeptId("group-b1")}),
        time_range=TimeRange(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
    )
    with pytest.raises(ForbiddenError):
        await fetch_resource_rows(adapter, "C1_listPieceworkRecords", query)

    await adapter.aclose()
    await client.aclose()
