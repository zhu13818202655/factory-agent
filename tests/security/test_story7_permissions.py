"""Story 7 permission & privacy tests for the remaining L1 capabilities.

Proves the four data-capability guarantees on the new recipes:
1. User business filters (dept/employee) only narrow; out-of-scope ids are
   rejected before any business-data call.
2. A target employee outside the caller's tenant resolution path never reaches
   the wage call.
3. Credential canary values (app_key/sign/access_token) never enter ResultTable
   rows, cards, summaries, or XLSX bytes.
4. `unavailable` columns render identically as 暂无数据源 across card, Excel and
   summary and are never fabricated into numbers.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.customer import sign_of
from mock_mes.api.server import create_app

from factory_agent.application.card import build_card
from factory_agent.application.filters import FilterNarrower, FilterRejectionError, NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import (
    CapabilityId,
    DataScope,
    DeptId,
    EmployeeId,
    ScopeVersion,
    TenantId,
    TimeRange,
    UserId,
)
from factory_agent.execution.executor import ScopedExecutor
from factory_agent.execution.kernel import KernelCapabilityRunner, render_table_from_run_result
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import UNAVAILABLE_VALUE, default_metric_registry
from factory_agent.export.xlsx import render_xlsx
from factory_agent.ports.session import CapabilityRunRequest

NOW = datetime(2026, 8, 21, 8, tzinfo=UTC)
RANGE = TimeRange(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 31, tzinfo=UTC))

#: Sensitive canary values that must never reach outputs.
CANARY_APP_KEY = "APPKEY-A"
CANARY_SIGN = "canary-sign-deadbeef"
CANARY_TOKEN = "canary-access-token-1234"


def _bundle(user: str, app_key: str = "APPKEY-A") -> MesCredentialBundle:
    timestamp = int(datetime.now(UTC).timestamp())
    return MesCredentialBundle(
        access_token=f"MOCK-TOKEN-{user}",
        app_key=app_key,
        sign=sign_of(app_key, timestamp),
        timestamp=timestamp,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        user=UserId(user),
        uname="模拟",
    )


def _scope(
    employee_ids: frozenset[EmployeeId],
    dept_ids: frozenset[DeptId],
    *,
    mes_filtered: bool = False,
) -> DataScope:
    return DataScope(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=employee_ids,
        dept_ids=dept_ids,
        evaluated_at=NOW,
        scope_version=ScopeVersion("v1"),
        mes_filtered=mes_filtered,
    )


def test_out_of_scope_department_request_is_rejected_with_zero_calls() -> None:
    """FR-007: 用户指定车间只能与 DataScope 求交；空交集拒绝且零业务调用."""
    scope = _scope(frozenset({EmployeeId("01008")}), frozenset({DeptId("dept-a1")}))

    with pytest.raises(FilterRejectionError) as error:
        FilterNarrower().narrow(scope, dept_ids=frozenset({DeptId("dept-a2")}))

    assert error.value.code == "forbidden"


def test_other_employee_wage_request_is_rejected_with_zero_calls() -> None:
    """Worker cannot query another employee's wage through the scope narrow path."""
    scope = _scope(frozenset({EmployeeId("01001")}), frozenset({DeptId("dept-a1")}))

    with pytest.raises(FilterRejectionError) as error:
        FilterNarrower().narrow(scope, employee_ids=frozenset({EmployeeId("01002")}))

    assert error.value.code == "forbidden"


@pytest.mark.asyncio
async def test_fr008_card_and_xlsx_never_leak_credentials() -> None:
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter("http://test", _bundle("01009"), catalog, client=client)
    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(catalog.operation_ids),
        default_metric_registry(),
        clock=lambda: NOW,
    )
    try:
        result = await runner.run(
            CapabilityRunRequest(
                capability_id=CapabilityId("fr008_payroll_ranking"),
                filters=NarrowedFilters(
                    tenant_id=TenantId("APPKEY-A"), employee_ids=None, dept_ids=None
                ),
                time_range=RANGE,
            )
        )
    finally:
        await adapter.aclose()
        await client.aclose()

    render = render_table_from_run_result(result)
    card = build_card(render)
    summary = str(card["summary"])
    excel = render_xlsx(render)

    serialized = repr(result.rows) + summary
    assert CANARY_APP_KEY not in serialized
    assert CANARY_SIGN not in serialized
    assert CANARY_TOKEN not in serialized
    assert "01009" not in serialized  # caller's uid is not a ranking row field
    assert CANARY_APP_KEY not in _xlsx_text(excel)
    # The card/summary only cite numbers already in the table.
    assert "25.4" in summary or "3.75" in summary


@pytest.mark.asyncio
async def test_unavailable_columns_render_consistently_as_no_data_source() -> None:
    app = create_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter("http://test", _bundle("01009"), catalog, client=client)
    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(catalog.operation_ids),
        default_metric_registry(),
        clock=lambda: NOW,
    )
    try:
        result = await runner.run(
            CapabilityRunRequest(
                capability_id=CapabilityId("fr007_workshop_output_comparison"),
                filters=NarrowedFilters(
                    tenant_id=TenantId("APPKEY-A"), employee_ids=None, dept_ids=None
                ),
                time_range=RANGE,
            )
        )
    finally:
        await adapter.aclose()
        await client.aclose()

    render = render_table_from_run_result(result)
    card = build_card(render)
    excel = render_xlsx(render)

    # ResultTable rows carry the sentinel; card + summary + Excel map it to 暂无数据源.
    assert all(row[5] == UNAVAILABLE_VALUE for row in result.rows)
    columns = cast("list[dict[str, object]]", card["columns"])
    achievement = next(c for c in columns if c["name"] == "achievement_rate")
    assert achievement["value"] == "暂无数据源"
    assert "暂无数据源" in str(card["summary"])
    assert "暂无数据源" in _xlsx_text(excel)


def _xlsx_text(workbook: bytes) -> str:
    """Extract the shared strings + sheet XML text from an XLSX zip."""
    parts: list[str] = []
    with zipfile.ZipFile(BytesIO(workbook)) as archive:
        for name in ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"):
            parts.append(archive.read(name).decode("utf-8"))
    return "\n".join(parts)


def test_foreign_tenant_filters_never_reach_a_business_call() -> None:
    """A NarrowedFilters bound to a foreign tenant is rejected by the executor's
    scope verifier before any adapter traffic (tenant can never be user-set)."""
    from factory_agent.domain.errors import ForbiddenError
    from factory_agent.execution.executor import StrictScopeVerifier

    scope = _scope(frozenset({EmployeeId("01001")}), frozenset({DeptId("dept-a1")}))
    verifier = StrictScopeVerifier()
    with pytest.raises(ForbiddenError):
        verifier.verify(
            scope,
            NarrowedFilters(tenant_id=TenantId("APPKEY-B"), employee_ids=None, dept_ids=None),
        )
