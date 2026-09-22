"""MES adapter metering: success/failure recording and the D13 pre-call guard.

Every MES HTTP attempt is recorded at the single ``_send`` exit (success and
failure); a disabled tenant must be rejected before any external request.
The recorder is a protocol, so this suite
injects a recording fake and asserts adapter behaviour without a database.
"""

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

    # The MES call succeeds even though recording failed.
    await adapter.execute(_request())
    await adapter.aclose()


@pytest.mark.asyncio
async def test_debug_capture_stores_envelope_as_structure_not_repr_string() -> None:
    """B-channel capture keeps the envelope's JSON structure.

    The envelope is a pydantic model; handed to the capture layer raw it would
    be flattened by ``json.dumps(..., default=str)`` into one giant repr
    string — rows counted as 0 and the report showing a repr blob. The capture
    must ``model_dump`` it: rows counted, nesting intact, JSON-renderable.
    """
    import json as _json

    from factory_agent.observability.debug_trace import (
        CaptureScope,
        close_capture_scope,
        configure_debug_trace,
        drain_debug_captures,
        open_capture_scope,
    )
    from tests.support.payload import as_dict, as_list

    configure_debug_trace(enabled=True, max_payload_bytes=262_144, max_rows=500)
    open_capture_scope(
        CaptureScope(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-1",
            interaction_id="interaction-1",
        )
    )
    rows = [
        {"id": 1, "uname": "王倩倩", "je": 1.08},
        {"id": 2, "uname": "郑金禄", "je": 2.20},
    ]
    adapter = _adapter(
        client=JsonBodyTransport(_envelope(result={"list": rows, "total": 2})).client(),
    )
    try:
        await adapter.execute(_request())
        captures = drain_debug_captures()
    finally:
        close_capture_scope()
        configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)

    assert captures, "capture was enabled: the MES span must have been captured"
    mes = [c for c in captures if c.kind == "mes"]
    assert mes, "the MES span capture is missing"
    payload = mes[-1].payload
    assert payload.truncated is False
    envelope = as_dict(as_dict(payload.output)["envelope"])
    assert envelope["code"] == 1
    result = as_dict(envelope["result"])
    out_rows = as_list(result["list"])
    assert len(out_rows) == 2
    assert as_dict(out_rows[0])["uname"] == "王倩倩"
    # The whole point: the stored payload must be JSON-serializable as-is —
    # a repr string would not be, and the report cannot render it as JSON.
    rendered = _json.dumps(payload.output, ensure_ascii=False)
    assert "王倩倩" in rendered
    assert "'uname'" not in rendered, "single quotes would mean a repr string, not JSON"
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
