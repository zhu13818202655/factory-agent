from __future__ import annotations

import pytest
from pydantic import BaseModel

from factory_agent.data_api.canonical import (
    CANONICAL_OPERATION_PATHS,
    CanonicalMesAdapter,
    CanonicalRequest,
)
from factory_agent.domain.errors import (
    ForbiddenError,
    InvalidRequestError,
    MesTimeoutError,
    NotFoundError,
    RateLimitedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    UpstreamInvalidError,
    UpstreamUnavailableError,
)


class _Item(BaseModel):
    record_id: str


class _Page(BaseModel):
    items: list[_Item]
    total: int
    page: int
    size: int


def test_unknown_operation_is_rejected_without_http() -> None:
    adapter = CanonicalMesAdapter("http://mock.invalid", "credential")
    request = CanonicalRequest(
        operation_id="X9_notRegistered",
        query=(("page", "1"),),
        response_model=_Page,
    )
    with pytest.raises(UnsupportedOperationError):
        # No client is configured; any HTTP attempt would fail differently.
        import asyncio

        asyncio.run(adapter.execute(request))


def test_operation_path_whitelist_covers_canonical_operations() -> None:
    expected = {
        "A1_getTenantMembership",
        "A2_listOrganizationAssignments",
        "A3_listEffectiveScopes",
        "C1_listPieceworkRecords",
        "C2_listEmployees",
        "C3_listDepartments",
        "C4_listOrders",
        "C5_listStyles",
        "C6_listOperations",
        "C7_listProductionPlans",
        "C8_listPayrollSettlements",
    }
    assert set(CANONICAL_OPERATION_PATHS) == expected
    assert all(path.startswith("/v1/") for path in CANONICAL_OPERATION_PATHS.values())


@pytest.mark.asyncio
async def test_error_status_mapping_to_unified_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support.http_stubs import StubTransport

    cases: list[tuple[int, type[Exception]]] = [
        (400, InvalidRequestError),
        (401, UnauthenticatedError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (502, UpstreamInvalidError),
        (503, UpstreamUnavailableError),
    ]
    for status, expected in cases:
        adapter = CanonicalMesAdapter(
            "http://mock.invalid", "credential", client=StubTransport(status).client()
        )
        with pytest.raises(expected):
            await adapter.execute(
                CanonicalRequest(
                    "A1_getTenantMembership", (("as_of", "2026-08-21T00:00:00Z"),), _Page
                )
            )
        await adapter.aclose()


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from tests.support.http_stubs import RaisingTransport

    adapter = CanonicalMesAdapter(
        "http://mock.invalid",
        "credential",
        settings=None,
        client=httpx.AsyncClient(
            transport=RaisingTransport(httpx.TimeoutException("t")), base_url="http://mock.invalid"
        ),
    )
    with pytest.raises(MesTimeoutError):
        await adapter.execute(CanonicalRequest("A1_getTenantMembership", (), _Page))
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rate_limited_respects_retry_after_and_retries() -> None:
    import httpx

    from tests.support.http_stubs import SequenceTransport

    transport = SequenceTransport([429, 200])
    adapter = CanonicalMesAdapter(
        "http://mock.invalid",
        "credential",
        client=httpx.AsyncClient(transport=transport, base_url="http://mock.invalid"),
    )
    result = await adapter.execute(CanonicalRequest("A1_getTenantMembership", (), _Page))
    assert isinstance(result, _Page)
    assert transport.requests == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rate_limited_exhausts_retries_with_structured_error() -> None:
    import httpx

    from tests.support.http_stubs import SequenceTransport

    transport = SequenceTransport([429, 429, 429])
    adapter = CanonicalMesAdapter(
        "http://mock.invalid",
        "credential",
        client=httpx.AsyncClient(transport=transport, base_url="http://mock.invalid"),
    )
    with pytest.raises(RateLimitedError) as error_info:
        await adapter.execute(CanonicalRequest("A1_getTenantMembership", (), _Page))
    assert error_info.value.retry_after_seconds == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_schema_drift_raises_upstream_invalid_without_payload_leak() -> None:

    from tests.support.http_stubs import JsonBodyTransport

    adapter = CanonicalMesAdapter(
        "http://mock.invalid",
        "credential",
        client=JsonBodyTransport(
            {"items": [{"record_id": {"nested": "drift"}}], "total": 1, "page": 1, "size": 1}
        ).client(),
    )
    with pytest.raises(UpstreamInvalidError) as error_info:
        await adapter.execute(CanonicalRequest("A1_getTenantMembership", (), _Page))
    assert "nested" not in str(error_info.value)
    await adapter.aclose()
