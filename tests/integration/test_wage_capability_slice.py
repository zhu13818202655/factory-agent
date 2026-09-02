"""Wage vertical slice against the real Mock MES in-process app.

Proves FR-002 (summary) and FR-003 (detail) share the ``GongziMxQuery`` path,
that the local aggregate reconciles against ``footer.je_total``, and that the
detail total equals the summary gross total — all offline with the Mock MES.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.customer import sign_of

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import CapabilityId, TimeRange, UserId
from factory_agent.execution.executor import ScopedExecutor
from factory_agent.execution.kernel import KernelCapabilityRunner
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.ports.session import CapabilityRunRequest


def _worker_bundle() -> MesCredentialBundle:
    timestamp = int(datetime.now(UTC).timestamp())
    return MesCredentialBundle(
        access_token="MOCK-TOKEN-01001",
        app_key="APPKEY-A",
        sign=sign_of("APPKEY-A", timestamp),
        timestamp=timestamp,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        user=UserId("01001"),
        uname="模拟员工甲",
    )


def _filters() -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=_worker_bundle().tenant_id,
        employee_ids=frozenset({_worker_bundle().employee_id}),
        dept_ids=frozenset(),
    )


def _range() -> TimeRange:
    return TimeRange(
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_fr002_and_fr003_slice_against_mock_mes(mock_mes_app: Any) -> None:
    client = AsyncClient(transport=ASGITransport(app=mock_mes_app), base_url="http://test")
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter("http://test", _worker_bundle(), catalog, client=client)
    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    runner = KernelCapabilityRunner(
        executor, load_recipes(catalog.operation_ids), default_metric_registry()
    )

    detail = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr003_personal_wage_detail"),
            filters=_filters(),
            time_range=_range(),
        )
    )
    summary = await runner.run(
        CapabilityRunRequest(
            capability_id=CapabilityId("fr002_personal_wage_summary"),
            filters=_filters(),
            time_range=_range(),
        )
    )

    assert detail.column_names == ("rq", "worktype", "sl", "price", "je")
    # The window is a real 500-person factory; 01001 has ~100 wage
    # rows in two months (mirrors the regenerated golden).
    assert len(detail.rows) == 94
    assert detail.totals["je"] == Decimal("573.60")
    assert detail.totals["sl"] == Decimal("559")
    assert summary.totals["gross_total"] == detail.totals["je"]
    assert summary.totals["piece_count"] == Decimal("559")
    assert summary.incomplete is False
    assert detail.incomplete is False

    await adapter.aclose()
    await client.aclose()
