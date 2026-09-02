"""MES adapter metering: success/failure recording and the D13 pre-call guard.

Story 11 2.6 requires every MES HTTP attempt to be recorded at the single
``_send`` exit (success and failure); 4.2 / 6.7 require a disabled tenant to be
rejected before any external request. The recorder is a protocol, so this suite
injects a recording fake and asserts adapter behaviour without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter, MesRequest
from factory_agent.domain import UserId
from factory_agent.domain.errors import TenantDisabledError
from factory_agent.ports import MesCallRecord, MesCallRecorder
from factory_agent.ports.tenant_registry import TenantRegistryReader, TenantRegistryRecord
from tests.support.http_stubs import JsonBodyTransport, RaisingTransport


@dataclass
class RecordingRecorder:
    calls: list[MesCallRecord] = field(default_factory=lambda: [])

    def record(self, call: MesCallRecord) -> None:
        self.calls.append(call)


@dataclass
class FakeRegistry:
    records: dict[str, TenantRegistryRecord] = field(default_factory=lambda: {})
    lookups: list[str] = field(default_factory=lambda: [])

    async def get(self, app_key: str) -> TenantRegistryRecord | None:
        self.lookups.append(app_key)
        return self.records.get(app_key)


def _bundle() -> MesCredentialBundle:
    return MesCredentialBundle(
        access_token="mock-access-token",
        app_key="APPKEY-A",
        sign="mock-sign",
        timestamp=1,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("01001"),
        uname="模拟员工甲",
    )


def _request() -> MesRequest:
    return MesRequest("YskQuery", {"Uid": "01001", "dates": "2026-07-01", "datee": "2026-08-31"})


def _envelope(code: int = 1, result: Any = None) -> dict[str, Any]:
    return {"code": code, "message": "成功", "result": result, "timestamp": 1}


def _adapter(
    *,
    recorder: MesCallRecorder | None = None,
    registry: TenantRegistryReader | None = None,
    client: httpx.AsyncClient | None = None,
) -> HongzhaoMesAdapter:
    return HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        load_catalog(),
        recorder=recorder,
        tenant_registry=registry,
        client=client,
    )


@pytest.mark.asyncio
async def test_successful_call_is_recorded_as_completed() -> None:
    recorder = RecordingRecorder()
    adapter = _adapter(
        recorder=recorder,
        client=JsonBodyTransport(_envelope(result={"list": [{"id": 1}], "total": 1})).client(),
    )

    await adapter.execute(_request())

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call.operation_id == "YskQuery"
    assert call.status == "completed"
    assert call.row_count == 1
    assert call.page_count == 1
    assert call.error_category is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_failed_call_is_recorded_as_failed() -> None:
    recorder = RecordingRecorder()
    adapter = _adapter(
        recorder=recorder,
        client=httpx.AsyncClient(
            transport=RaisingTransport(httpx.ConnectError("boom")),
            base_url="http://mock.invalid",
        ),
    )

    with pytest.raises(Exception):
        await adapter.execute(_request())

    # Every transport failure attempt is metered as failed (default retries
    # mean several records); all carry the failed status and an error category.
    assert len(recorder.calls) >= 1
    assert all(call.status == "failed" for call in recorder.calls)
    assert all(call.operation_id == "YskQuery" for call in recorder.calls)
    assert all(call.error_category is not None for call in recorder.calls)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_no_recorder_means_no_metering_and_no_effect() -> None:
    adapter = _adapter(
        client=JsonBodyTransport(_envelope(result={"list": [], "total": 0})).client(),
    )

    await adapter.execute(_request())

    # No recorder configured: the adapter still works (e.g. readiness probes).
    await adapter.aclose()


@pytest.mark.asyncio
async def test_recorder_raising_never_breaks_the_mes_call() -> None:
    class ExplodingRecorder:
        def record(self, call: MesCallRecord) -> None:
            raise RuntimeError("recorder broke")

    adapter = _adapter(
        recorder=ExplodingRecorder(),  # type: ignore[abstract]
        client=JsonBodyTransport(_envelope(result={"list": [], "total": 0})).client(),
    )

    # The MES call succeeds even though recording failed (Story 11 1.6 / 2.6).
    await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_disabled_tenant_is_rejected_before_any_http_call() -> None:
    recorder = RecordingRecorder()
    registry = FakeRegistry(
        records={"APPKEY-A": TenantRegistryRecord("APPKEY-A", "工厂甲", "disabled")}
    )
    adapter = _adapter(recorder=recorder, registry=registry)

    with pytest.raises(TenantDisabledError):
        await adapter.execute(_request())

    # No HTTP attempt was made, so nothing was recorded, and the adapter never
    # touched the wire (the transport would raise if used).
    assert registry.lookups == ["APPKEY-A"]
    assert recorder.calls == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_enabled_tenant_is_allowed_to_call() -> None:
    recorder = RecordingRecorder()
    registry = FakeRegistry(
        records={"APPKEY-A": TenantRegistryRecord("APPKEY-A", "工厂甲", "active")}
    )
    adapter = _adapter(
        recorder=recorder,
        registry=registry,
        client=JsonBodyTransport(_envelope(result={"list": [], "total": 0})).client(),
    )

    await adapter.execute(_request())

    assert len(recorder.calls) == 1
    await adapter.aclose()


@pytest.mark.asyncio
async def test_unknown_appkey_is_treated_as_allowed_when_registry_is_absent() -> None:
    """Degradation: without a registry the guard is skipped, not defaulted to deny."""
    adapter = _adapter(
        client=JsonBodyTransport(_envelope(result={"list": [], "total": 0})).client(),
    )

    await adapter.execute(_request())
    await adapter.aclose()
