"""Full-chain fault recovery against Mock MES fault injection.

Drives the same recipe -> executor -> sandbox -> ResultTable golden-slice path, but
under customer-shaped faults: 429 / 5xx transport faults,
``code=0`` credential errors, footer disagreement, and pagination drift. Every
fault must surface as a structured failure or an explicit incomplete state —
never a fabricated number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mock_mes.api.customer import sign_of

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import (
    CapabilityId,
    EmployeeId,
    MesError,
    NarrowedFilters,
    TenantId,
    TimeRange,
    UserId,
)
from factory_agent.execution.executor import ScopedExecutor
from factory_agent.execution.kernel import KernelCapabilityRunner, KernelSettings
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.ports.session import CapabilityRunRequest

NOW = datetime(2026, 8, 21, 8, tzinfo=UTC)
RANGE = TimeRange(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 31, tzinfo=UTC))


class FaultInjectingTransport(httpx.AsyncBaseTransport):
    """Injects ``X-Mock-Fault`` and extra fault headers on every request."""

    def __init__(self, fault: str, app: Any, **headers: str) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self._fault = fault
        self._headers = headers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["X-Mock-Fault"] = self._fault
        for name, value in self._headers.items():
            request.headers[name] = value
        return await self._inner.handle_async_request(request)


def _bundle(user: str) -> MesCredentialBundle:
    timestamp = int(datetime.now(UTC).timestamp())
    return MesCredentialBundle(
        access_token=f"MOCK-TOKEN-{user}",
        app_key="APPKEY-A",
        sign=sign_of("APPKEY-A", timestamp),
        timestamp=timestamp,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        user=UserId(user),
        uname="模拟",
    )


def _runner(
    app: Any, fault: str, **headers: str
) -> tuple[KernelCapabilityRunner, HongzhaoMesAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=FaultInjectingTransport(fault, app, **headers), base_url="http://test"
    )
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter("http://test", _bundle("01009"), catalog, client=client)
    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(catalog.operation_ids),
        default_metric_registry(),
        settings=KernelSettings(page_size=2000, max_api_calls=300),
        clock=lambda: NOW,
    )
    return runner, adapter, client


def _own_output_filters() -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId("01001")}),
        dept_ids=None,
    )


async def _run(runner: KernelCapabilityRunner, cid: str) -> Any:
    return await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId(cid),
            filters=_own_output_filters(),
            time_range=RANGE,
        )
    )


@pytest.mark.asyncio
async def test_transport_429_fault_surfaces_as_structured_error_not_a_number(
    mock_mes_app: Any,
) -> None:
    runner, adapter, client = _runner(mock_mes_app, "429")
    try:
        with pytest.raises(MesError):
            await _run(runner, "fr002_personal_wage_summary")
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_5xx_fault_surfaces_as_structured_error(mock_mes_app: Any) -> None:
    runner, adapter, client = _runner(mock_mes_app, "5xx")
    try:
        with pytest.raises(MesError):
            await _run(runner, "fr002_personal_wage_summary")
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_footer_mismatch_is_reported_as_reconciliation_failed(mock_mes_app: Any) -> None:
    runner, adapter, client = _runner(
        mock_mes_app, "footer_mismatch", **{"X-Mock-Footer-Field": "je_total"}
    )
    try:
        result = await _run(runner, "fr002_personal_wage_summary")
    finally:
        await adapter.aclose()
        await client.aclose()

    assert result.incomplete is True
    assert result.incomplete_reason == "reconciliation_failed"
    # The local sum is kept, but the answer is never presented as complete.
    assert any("footer" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_pagination_total_drift_is_reported_as_incomplete(mock_mes_app: Any) -> None:
    runner, adapter, client = _runner(mock_mes_app, "wrong_total")
    try:
        result = await _run(runner, "fr002_personal_wage_summary")
    finally:
        await adapter.aclose()
        await client.aclose()

    assert result.incomplete is True
    assert (result.incomplete_reason or "").startswith("pagination_")
