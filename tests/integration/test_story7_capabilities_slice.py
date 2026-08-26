"""Story 7 vertical slice: the remaining 9 L1 capabilities against Mock MES.

Runs FR-001, FR-005, FR-006, FR-007, FR-008, FR-009, FR-010, FR-011 and
FR-012 through the same recipe -> executor -> sandbox -> ResultTable path as
Story 6's FR-002/FR-003, and locks the deterministic golden numbers. A boss
identity (move_admin_role=01) sees the whole COMPANY-A range via MES row-level
filtering; employee_ids=None on management capabilities lets MES decide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.customer import sign_of
from mock_mes.api.server import create_app

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import (
    CapabilityId,
    EmployeeId,
    NarrowedFilters,
    TenantId,
    TimeRange,
    UserId,
)
from factory_agent.execution.executor import ScopedExecutor
from factory_agent.execution.kernel import KernelCapabilityRunner
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import UNAVAILABLE_VALUE, default_metric_registry
from factory_agent.ports.session import CapabilityRunRequest

NOW = datetime(2026, 8, 21, 8, tzinfo=UTC)
RANGE = TimeRange(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 31, tzinfo=UTC))


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


def _management_filters() -> NarrowedFilters:
    """Boss view: no employee-level restriction; MES decides the range."""
    return NarrowedFilters(tenant_id=TenantId("APPKEY-A"), employee_ids=None, dept_ids=None)


def _runner() -> tuple[KernelCapabilityRunner, HongzhaoMesAdapter, AsyncClient]:
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
    return runner, adapter, client


async def _run(runner: KernelCapabilityRunner, cid: str, filters: NarrowedFilters):
    return await runner.run(
        CapabilityRunRequest(capability_id=CapabilityId(cid), filters=filters, time_range=RANGE)
    )


@pytest.mark.asyncio
async def test_fr001_personal_output_worker_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(
            runner,
            "fr001_personal_output",
            NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=frozenset({EmployeeId("01001")}),
                dept_ids=None,
            ),
        )
        assert result.column_names == ("rq", "huohao", "worktype", "output_qty", "defective_qty")
        assert len(result.rows) == 4
        assert result.totals["output_qty"] == Decimal("20")
        # 合格/次品无统一数据源（C.5）：列级 unavailable，绝不渲染为数字。
        for row in result.rows:
            assert row[4] == UNAVAILABLE_VALUE
        assert result.incomplete is True
        assert result.incomplete_reason == "metric_unavailable:quality_defective"
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr005_order_progress_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr005_order_progress", _management_filters())
        rows = {row[0]: row for row in result.rows}
        # PLAN-2607-001: 100 计划, 13 完成, 2/3 工序, 无当前工序, 在制 93.
        assert rows["PLAN-2607-001"] == (
            "PLAN-2607-001",
            "HH001",
            Decimal("100"),
            Decimal("13"),
            "0.6666666666666666",
            None,
            Decimal("93"),
        )
        # PLAN-2608-001: 50 计划, 3 完成, 1/3 工序, 当前工序=手工钉扣, 无在制数据.
        assert rows["PLAN-2608-001"] == (
            "PLAN-2608-001",
            "HH001",
            Decimal("50"),
            Decimal("3"),
            "0.3333333333333333",
            "手工钉扣",
            UNAVAILABLE_VALUE,
        )
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr005_order_progress_exact_order_filter() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(
            runner,
            "fr005_order_progress",
            NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                order_codes=frozenset({"PLAN-2607-001"}),
            ),
        )
        assert len(result.rows) == 1
        assert result.rows[0][0] == "PLAN-2607-001"
        # 未知/跨租户订单精确匹配返回空（M12：无数据），不伪造。
        empty = await _run(
            runner,
            "fr005_order_progress",
            NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=None,
                dept_ids=None,
                order_codes=frozenset({"KHDD-DOES-NOT-EXIST"}),
            ),
        )
        assert len(empty.rows) == 0
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr006_order_output_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr006_order_output", _management_filters())
        rows = {(row[0], row[1]): row for row in result.rows}
        # HH001 x WT01: 12 产量, 2 参与人; HH001 x WT03: 4 产量, 1 参与人.
        assert rows[("HH001", "WT01")][2] == Decimal("12")
        assert rows[("HH001", "WT01")][3] == Decimal("2")
        assert rows[("HH001", "WT03")][2] == Decimal("4")
        assert rows[("HH001", "WT03")][3] == Decimal("1")
        assert result.totals["output_qty"] == Decimal("16")
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr007_workshop_comparison_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr007_workshop_output_comparison", _management_filters())
        by_name = {row[0]: row for row in result.rows}
        # 一车间 13 件 / 1 人 / 人均 13 / 第 1; 二车间 3 件 / 1 人 / 人均 3 / 第 2.
        assert by_name["一车间"][1] == Decimal("13")
        assert by_name["一车间"][2] == Decimal("1")
        assert by_name["一车间"][3] == Decimal("13")
        assert by_name["一车间"][4] == Decimal("1")
        assert by_name["二车间"][4] == Decimal("2")
        # 达成率按 C.9 输出 unavailable.
        assert all(row[5] == UNAVAILABLE_VALUE for row in result.rows)
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr008_payroll_ranking_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr008_payroll_ranking", _management_filters())
        # 位次按返回顺序编号（M7）；同名员工以 uid 区分。
        assert result.rows[0] == (
            "01001",
            "模拟员工甲",
            "dept-a1",
            Decimal("5"),
            Decimal("21.65"),
            Decimal("1"),
        )
        assert result.rows[1] == (
            "01002",
            "模拟员工甲",
            "dept-a2",
            Decimal("1"),
            Decimal("3.75"),
            Decimal("2"),
        )
        assert result.totals["gross"] == Decimal("25.40")
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr009_factory_order_overview_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr009_factory_order_overview", _management_filters())
        rows = {row[0]: row for row in result.rows}
        # K4 交期状态：finish_date 早于当前日期 → 已逾期（无预警阈值）。
        assert rows["PLAN-2607-001"][5] == "已逾期"
        assert rows["PLAN-2608-001"][5] == "已逾期"
        assert result.totals["plan_qty"] == Decimal("150")
        assert result.totals["completed_qty"] == Decimal("16")
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr010_workshop_output_overview_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr010_workshop_output_overview", _management_filters())
        rows = {(row[0], row[1]): row for row in result.rows}
        # 一车间 HH001: 100 计划 / 13 完成; 零计划 PLAN-2608-002 行也呈现.
        assert rows[("一车间", "HH001")][2] == Decimal("100")
        assert rows[("一车间", "HH001")][3] == Decimal("13")
        assert rows[("一车间", "HH002")][2] == Decimal("0")
        # 量产状态（C.8）与达成率（C.9）均 unavailable。
        assert rows[("一车间", "HH001")][4] == UNAVAILABLE_VALUE
        assert rows[("一车间", "HH001")][5] == UNAVAILABLE_VALUE
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr011_factory_payroll_stats_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(runner, "fr011_factory_payroll_stats", _management_filters())
        by_name = {row[0]: row for row in result.rows}
        assert by_name["一车间"][1] == Decimal("21.65")
        assert by_name["一车间"][2] == Decimal("1")
        assert by_name["二车间"][1] == Decimal("3.75")
        # 在册人数（C.7）与人均工资均 unavailable。
        assert by_name["一车间"][3] == UNAVAILABLE_VALUE
        assert by_name["一车间"][4] == UNAVAILABLE_VALUE
        assert result.totals["gross_total"] == Decimal("25.40")
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_fr012_employee_payroll_golden() -> None:
    runner, adapter, client = _runner()
    try:
        result = await _run(
            runner,
            "fr012_employee_payroll",
            NarrowedFilters(
                tenant_id=TenantId("APPKEY-A"),
                employee_ids=frozenset({EmployeeId("01001")}),
                dept_ids=None,
            ),
        )
        assert result.rows[0] == (Decimal("21.65"), Decimal("20"))
        assert result.incomplete is False
    finally:
        await adapter.aclose()
        await client.aclose()
