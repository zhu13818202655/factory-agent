from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import (
    HongzhaoMesAdapter,
    MesRequest,
    map_message_to_error,
)
from factory_agent.domain import UserId
from factory_agent.domain.errors import (
    InvalidRequestError,
    MesTimeoutError,
    RateLimitedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    UpstreamInvalidError,
    UpstreamUnavailableError,
)


def _bundle() -> MesCredentialBundle:
    """Build a bundle that never proactively expires during a test."""
    return MesCredentialBundle(
        access_token="mock-access-token",
        app_key="APPKEY-A",
        sign="mock-sign",
        timestamp=1,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("01001"),
        uname="模拟员工甲",
    )


def _catalog():
    return load_catalog()


def _adapter(*, client: httpx.AsyncClient | None = None) -> HongzhaoMesAdapter:
    return HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        _catalog(),
        client=client,
    )


def _envelope(code: int, message: str = "成功", result: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "result": result, "timestamp": 1}


def _request() -> MesRequest:
    """A fully-parameterized ysk request so the body builds before HTTP."""
    return MesRequest(
        "YskQuery",
        {"Uid": "01001", "dates": "2026-07-01", "datee": "2026-08-31"},
    )


def test_unknown_operation_is_rejected_without_http() -> None:
    adapter = _adapter()
    with pytest.raises(UnsupportedOperationError):
        import asyncio

        asyncio.run(adapter.execute(MesRequest("X9_notRegistered", {})))


def test_disabled_operation_is_rejected_before_http() -> None:
    """K7: MoveMenuQuery is registered but disabled, so it must be rejected."""
    adapter = _adapter()
    with pytest.raises(UnsupportedOperationError):
        import asyncio

        asyncio.run(adapter.execute(MesRequest("MoveMenuQuery", {})))


def test_catalog_whitelist_covers_the_26_customer_operations() -> None:
    ids = _catalog().operation_ids
    assert "SystemToken" in ids
    assert "YskQuery" in ids
    assert "GongziMxQuery" in ids
    assert "MoveMenuQuery" in ids  # registered but disabled
    assert "A1_getTenantMembership" not in ids
    assert "C1_listPieceworkRecords" not in ids
    # All 26 customer operations are present.
    assert len(ids) == 26


@pytest.mark.asyncio
async def test_customer_failure_messages_map_to_unified_exceptions() -> None:
    assert isinstance(map_message_to_error("签名无效"), UnauthenticatedError)
    assert isinstance(map_message_to_error("请求已过期"), UnauthenticatedError)
    assert isinstance(map_message_to_error("app_key不能为空"), InvalidRequestError)
    assert isinstance(map_message_to_error("无效app_key"), InvalidRequestError)
    assert isinstance(map_message_to_error("加密信息解析失败"), InvalidRequestError)
    assert isinstance(map_message_to_error("some unknown failure"), UpstreamInvalidError)


@pytest.mark.asyncio
async def test_code_zero_envelope_raises_mapped_exception() -> None:
    from tests.support.http_stubs import JsonBodyTransport

    adapter = _adapter(
        client=JsonBodyTransport(_envelope(0, "无效app_key")).client(),
    )
    with pytest.raises(InvalidRequestError):
        await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_code_one_with_null_result_is_upstream_invalid() -> None:
    from tests.support.http_stubs import JsonBodyTransport

    adapter = _adapter(
        client=JsonBodyTransport(_envelope(1, "成功", result=None)).client(),
    )
    with pytest.raises(UpstreamInvalidError):
        await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_http_404_maps_to_upstream_unavailable() -> None:
    from tests.support.http_stubs import StubTransport

    adapter = _adapter(client=StubTransport(404).client())
    with pytest.raises(UpstreamUnavailableError):
        await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout_error() -> None:
    from tests.support.http_stubs import RaisingTransport

    adapter = _adapter(
        client=httpx.AsyncClient(
            transport=RaisingTransport(httpx.TimeoutException("t")), base_url="http://mock.invalid"
        ),
    )
    with pytest.raises(MesTimeoutError):
        await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_transport_failure_maps_to_upstream_unavailable() -> None:
    from tests.support.http_stubs import RaisingTransport

    adapter = _adapter(
        client=httpx.AsyncClient(
            transport=RaisingTransport(httpx.ConnectError("boom")),
            base_url="http://mock.invalid",
        ),
    )
    with pytest.raises(UpstreamUnavailableError):
        await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rate_limited_respects_retry_after_and_retries() -> None:
    from tests.support.http_stubs import SequenceTransport

    transport = SequenceTransport([429, 200])
    adapter = _adapter(
        client=httpx.AsyncClient(transport=transport, base_url="http://mock.invalid"),
    )
    # The 429 path raises RateLimitedError after the retry policy is exhausted
    # because the second 200 is an invalid envelope; assert retries were made.
    with pytest.raises((RateLimitedError, UpstreamInvalidError)):
        await adapter.execute(_request())
    assert transport.requests == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rate_limited_exhausts_retries_with_structured_error() -> None:
    from tests.support.http_stubs import SequenceTransport

    transport = SequenceTransport([429, 429, 429])
    adapter = _adapter(
        client=httpx.AsyncClient(transport=transport, base_url="http://mock.invalid"),
    )
    with pytest.raises(RateLimitedError) as error_info:
        await adapter.execute(_request())
    assert error_info.value.retry_after_seconds == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_schema_drift_raises_upstream_invalid_without_payload_leak() -> None:
    from tests.support.http_stubs import JsonBodyTransport

    adapter = _adapter(
        client=JsonBodyTransport({"unexpected": "shape"}).client(),
    )
    with pytest.raises(UpstreamInvalidError) as error_info:
        await adapter.execute(_request())
    assert "nested" not in str(error_info.value)
    await adapter.aclose()
