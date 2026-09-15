

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


def test_catalog_whitelist_covers_the_30_customer_operations() -> None:
    ids = _catalog().operation_ids
    assert "SystemToken" in ids
    assert "YskQuery" in ids
    assert "GongziMxQuery" in ids
    assert "ScjdQuery" in ids
    assert "MoveMenuQuery" in ids  # registered but disabled
    assert "A1_getTenantMembership" not in ids
    assert "C1_listPieceworkRecords" not in ids
    # All 30 customer operations are present.
    assert len(ids) == 30


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


@pytest.mark.asyncio
async def test_all_operations_send_business_params_flat() -> None:
    """Every operation sends business params top-level flat (no ``param`` wrapper).

    Real customer MES 2026-09-06 (all families): a wrapped ``param`` body is
    silently ignored and returns canned pages with constant tenant totals, so
    the adapter never wraps; flat bodies are the customer contract.
    """
    import json as _json

    recorded: list[dict[str, Any]] = []

    class RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            recorded.append(_json.loads(request.content))
            return httpx.Response(
                200,
                json=_envelope(1, "成功", result={"list": [], "total": 0}),
                request=request,
            )

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=RecordingTransport(), base_url="http://mock.invalid")

    gongzi = HongzhaoMesAdapter("http://mock.invalid", _bundle(), _catalog(), client=_client())
    await gongzi.execute(
        MesRequest(
            "GongziMxQuery",
            {
                "Flag": "0",
                "Type": "0,1,2",
                "scheme": "",
                "queryFooter": True,
                "dates": "2026-09-01",
                "datee": "2026-09-05",
                "Uid": "01001",
                "page": 1,
                "size": 200,
            },
        )
    )
    flat = recorded[-1]
    assert "param" not in flat
    assert flat["app_key"] == "APPKEY-A"
    assert flat["dates"] == "2026-09-01"
    assert flat["Uid"] == "01001"
    assert flat["page"] == 1
    await gongzi.aclose()

    recorded.clear()
    ysk = HongzhaoMesAdapter("http://mock.invalid", _bundle(), _catalog(), client=_client())
    await ysk.execute(
        MesRequest("YskQuery", {"Uid": "01001", "dates": "2026-07-01", "datee": "2026-08-31"})
    )
    flat_ysk = recorded[-1]
    assert "param" not in flat_ysk
    assert flat_ysk["Uid"] == "01001"
    assert flat_ysk["dates"] == "2026-07-01"
    assert flat_ysk["app_key"] == "APPKEY-A"
    await ysk.aclose()


def _summary_request(*, scheme: str, uid: str = "01001") -> MesRequest:
    return MesRequest(
        "GongziMxQuery",
        {
            "Flag": "0",
            "Type": "0,1,2",
            "scheme": scheme,
            "queryFooter": True,
            "dates": "2026-09-01",
            "datee": "2026-09-05",
            "Uid": uid,
            "page": 1,
            "size": 200,
        },
    )


_NRE = "Object reference not set to an instance of an object."


@pytest.mark.asyncio
async def test_gongzi_mx_summary_empty_window_nre_is_absorbed_as_empty() -> None:
    """Ledger #21: summary over an empty window returns a .NET NRE (code=0)
    instead of zero rows; the adapter absorbs that exact case as an empty page
    so the product can answer "本月无计件数据" rather than failing the run."""
    from tests.support.http_stubs import JsonBodyTransport

    adapter = _adapter(
        client=JsonBodyTransport(_envelope(0, _NRE)).client(),
    )
    result = await adapter.execute(_summary_request(scheme="hz"))
    assert result.result == {"list": [], "total": 0}
    assert result.footer is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_gongzi_mx_detail_same_nre_message_still_raises() -> None:
    """The empty-window absorption is summary-only; detail mode keeps failing."""
    from tests.support.http_stubs import JsonBodyTransport

    adapter = _adapter(
        client=JsonBodyTransport(_envelope(0, _NRE)).client(),
    )
    with pytest.raises(UpstreamInvalidError):
        await adapter.execute(_summary_request(scheme=""))
    await adapter.aclose()


@pytest.mark.asyncio
async def test_gongzi_mx_query_normalizes_query_footer_to_boolean() -> None:
    """queryFooter arrives as the recipe string "1" but must be sent as a real
    boolean; some tenant backends reject the string form as a schema error
    ("请求参数缺少app_key、timestamp、sign", real MES 2026-09-06)."""
    import json as _json

    recorded: list[dict[str, Any]] = []

    class RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            recorded.append(_json.loads(request.content))
            return httpx.Response(
                200,
                json=_envelope(1, "成功", result={"list": [], "total": 0}),
                request=request,
            )

    adapter = HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        _catalog(),
        client=httpx.AsyncClient(transport=RecordingTransport(), base_url="http://mock.invalid"),
    )
    await adapter.execute(
        MesRequest(
            "GongziMxQuery",
            {
                "Flag": "0",
                "Type": "0,1,2",
                "scheme": "",
                "queryFooter": "1",
                "dates": "2026-09-01",
                "datee": "2026-09-05",
                "Uid": "01001",
                "page": 1,
                "size": 200,
            },
        )
    )
    flat = recorded[-1]
    assert flat["queryFooter"] is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_gongzi_mx_summary_other_code_zero_message_still_raises() -> None:
    """A non-NRE upstream failure on the summary path must not be swallowed."""
    from tests.support.http_stubs import JsonBodyTransport

    adapter = _adapter(
        client=JsonBodyTransport(_envelope(0, "some other upstream failure")).client(),
    )
    with pytest.raises(UpstreamInvalidError):
        await adapter.execute(_summary_request(scheme="hz"))
    await adapter.aclose()
