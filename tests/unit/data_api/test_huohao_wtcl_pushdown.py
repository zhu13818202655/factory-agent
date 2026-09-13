"""HuohaoWtCLQuery filter-pushdown tests (接口文档 §7.2 实测口径).

A single style code pushes down as the server-side ``huohao`` filter (取值是
款号 bbreed，与 ``NarrowedFilters.style_codes`` 同口径); multiple codes stay a
full fetch filtered by the reviewed local compute, and BarcodeClQuery is never
pushed down (its server ignores the filter silently, §7.1).
"""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import TenantId, UserId


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
        user=UserId("1642"),
        uname="汪云梅",
    )


def _filters(style_codes: frozenset[str]) -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=None,
        dept_ids=None,
        style_codes=style_codes,
    )


def _range() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 31, tzinfo=UTC),
    )


def _adapter(transport: _CapturingTransport) -> HongzhaoMesAdapter:
    return HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        load_catalog(),
        client=httpx.AsyncClient(transport=transport, base_url="http://mock.invalid"),
    )


_EMPTY_ENVELOPE: dict[str, Any] = {
    "code": 1,
    "message": "成功",
    "result": {"list": [], "total": 0},
    "timestamp": 1,
}

_REVIEWED_PARAMS = {"scheme": "货号工序", "queryFooter": "1"}


@pytest.mark.asyncio
async def test_single_style_code_pushes_down_huohao() -> None:
    transport = _CapturingTransport(_EMPTY_ENVELOPE)
    adapter = _adapter(transport)

    await adapter.fetch_resource(
        "HuohaoWtCLQuery", _filters(frozenset({"86B"})), _range(), 2000, _REVIEWED_PARAMS
    )

    assert transport.requests, "no MES request was sent"
    assert transport.requests[0]["huohao"] == "86B"


@pytest.mark.asyncio
async def test_multiple_style_codes_stay_local() -> None:
    transport = _CapturingTransport(_EMPTY_ENVELOPE)
    adapter = _adapter(transport)

    await adapter.fetch_resource(
        "HuohaoWtCLQuery",
        _filters(frozenset({"86B", "G2601"})),
        _range(),
        2000,
        _REVIEWED_PARAMS,
    )

    assert transport.requests, "no MES request was sent"
    assert "huohao" not in transport.requests[0]


@pytest.mark.asyncio
async def test_no_style_codes_sends_no_filter() -> None:
    transport = _CapturingTransport(_EMPTY_ENVELOPE)
    adapter = _adapter(transport)

    await adapter.fetch_resource(
        "HuohaoWtCLQuery", _filters(frozenset()), _range(), 2000, _REVIEWED_PARAMS
    )

    assert transport.requests, "no MES request was sent"
    assert "huohao" not in transport.requests[0]


@pytest.mark.asyncio
async def test_barcode_cl_query_is_never_pushed_down() -> None:
    transport = _CapturingTransport(_EMPTY_ENVELOPE)
    adapter = _adapter(transport)

    await adapter.fetch_resource("BarcodeClQuery", _filters(frozenset({"86B"})), _range(), 2000)

    assert transport.requests, "no MES request was sent"
    assert "huohao" not in transport.requests[0]
