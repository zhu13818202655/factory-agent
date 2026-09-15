"""MES 时间窗参数的本地日期换算回归测试。

Canonical 时间窗以 UTC 存储（``intent.py`` 把槽位时间转 UTC），而客户 MES 的
``dates``/``datee`` 是**本地（厂区时区）含端日历日期**。直接对 UTC 值取
``.date()`` 会把窗口整体前移一天（2026-08-01T00:00+08:00 → dates=2026-07-31，
2026-09-16 线上实测），修复后必须先换算到厂区时区再取日期。
"""

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import AdapterSettings, HongzhaoMesAdapter
from factory_agent.domain import TenantId, UserId

_FACTORY_ZONE = ZoneInfo("Asia/Shanghai")


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Record every JSON request body and answer with a fixed envelope."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(dict(json.loads(request.content)))
        return httpx.Response(200, json=self._body, request=request)


def _bundle() -> MesCredentialBundle:
    return MesCredentialBundle(
        access_token="mock-access-token",
        app_key="APPKEY-A",
        sign="mock-sign",
        timestamp=1,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("6"),
        uname="尚",
    )


def _filters() -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=None,
        dept_ids=None,
    )


_EMPTY_ENVELOPE: dict[str, Any] = {
    "code": 1,
    "message": "成功",
    "result": {"list": [], "total": 0},
    "timestamp": 1,
}


def _adapter(
    transport: _CapturingTransport, settings: AdapterSettings | None = None
) -> HongzhaoMesAdapter:
    return HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        load_catalog(),
        settings=settings,
        client=httpx.AsyncClient(transport=transport, base_url="http://mock.invalid"),
    )


async def _capture_dates(
    time_range: tuple[datetime, datetime], settings: AdapterSettings | None = None
) -> tuple[str, str]:
    transport = _CapturingTransport(_EMPTY_ENVELOPE)
    adapter = _adapter(transport, settings)
    await adapter.fetch_resource("YskQuery", _filters(), time_range, 2000)
    assert transport.requests, "no MES request was sent"
    return transport.requests[0]["dates"], transport.requests[0]["datee"]


@pytest.mark.asyncio
async def test_utc_window_maps_to_factory_local_dates() -> None:
    """线上实测回归：UTC 值直取 .date() 曾把 8 月窗口发成 07-31~08-31。"""
    window = (
        datetime(2026, 7, 31, 16, 0, tzinfo=UTC),  # = 2026-08-01T00:00+08:00
        datetime(2026, 8, 31, 16, 0, tzinfo=UTC),  # = 2026-09-01T00:00+08:00
    )
    assert await _capture_dates(window) == ("2026-08-01", "2026-08-31")


@pytest.mark.asyncio
async def test_half_open_end_yields_the_inclusive_last_local_day() -> None:
    """[08-01, 09-01) 本地半开窗 → datee 必须是含端的 08-31，不能是 09-01。"""
    window = (
        datetime(2026, 8, 1, tzinfo=_FACTORY_ZONE),
        datetime(2026, 9, 1, tzinfo=_FACTORY_ZONE),
    )
    assert await _capture_dates(window) == ("2026-08-01", "2026-08-31")


@pytest.mark.asyncio
async def test_same_day_window_maps_to_a_single_date() -> None:
    window = (
        datetime(2026, 8, 5, tzinfo=_FACTORY_ZONE),
        datetime(2026, 8, 6, tzinfo=_FACTORY_ZONE),
    )
    assert await _capture_dates(window) == ("2026-08-05", "2026-08-05")


@pytest.mark.asyncio
async def test_window_boundary_inside_the_local_day_stays_in_the_window() -> None:
    """end 落在本地当天中间时（UTC 换算后日期会前移），datee 仍须含该天。"""
    window = (
        datetime(2026, 8, 1, 0, 0, tzinfo=_FACTORY_ZONE),
        datetime(2026, 8, 10, 12, 0, tzinfo=_FACTORY_ZONE),  # = 08-10T04:00Z
    )
    assert await _capture_dates(window) == ("2026-08-01", "2026-08-10")


@pytest.mark.asyncio
async def test_injected_timezone_setting_is_honored() -> None:
    settings = AdapterSettings(factory_timezone="UTC")
    window = (
        datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )
    assert await _capture_dates(window, settings) == ("2026-08-01", "2026-08-02")
