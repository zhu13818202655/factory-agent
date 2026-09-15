"""``S3ArtifactStore`` against an in-memory fake gateway.

The fake session stands in for the untyped ``aioboto3`` client so the store's
error mapping is exercised without a live object store: a missing object stays
an ordinary miss, a corrupt sidecar stays a miss, and a write that never
landed fails the export instead of handing out a dead download id.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[reportMissingTypeStubs]

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.export.s3_store import S3ArtifactStore
from factory_agent.export_service import ExportService
from factory_agent.ports.artifacts import ErrorCatalog, ExportError, StoredArtifact
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_TENANT = TenantId("APPKEY-A")
_OWNER = InteractionOwner(tenant_id=_TENANT, user_id=UserId("01001"))
_NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


def _artifact(artifact_id: str) -> StoredArtifact:
    return StoredArtifact(
        artifact_id=artifact_id,
        tenant_id=str(_TENANT),
        user_id="01001",
        filename="管理_FR-008_2026-08-01_2026-08-31_20260914080000.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=b"PK\x03\x04xlsx-bytes",
        expires_at=_NOW + timedelta(days=7),
    )


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


class _FakeGateway:
    """Keyed object dictionary plus the failure switches each test flips."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_failures = 0
        self.delete_failure = False
        self.put_calls: list[str] = []


class _FakeClient:
    def __init__(self, gateway: _FakeGateway) -> None:
        self._gateway = gateway

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803 - the S3 API's own keyword names
        self._gateway.put_calls.append(Key)
        if self._gateway.put_failures:
            self._gateway.put_failures -= 1
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        self._gateway.objects[Key] = Body

    async def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self._gateway.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _FakeBody(self._gateway.objects[Key])}

    async def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        if self._gateway.delete_failure:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
        self._gateway.objects.pop(Key, None)


class _FakeSession:
    def __init__(self, gateway: _FakeGateway) -> None:
        self._gateway = gateway

    def client(self, service: str, **kwargs: object) -> _FakeClient:
        return _FakeClient(self._gateway)


def _store(gateway: _FakeGateway) -> S3ArtifactStore:
    return S3ArtifactStore(
        endpoint_url="http://127.0.0.1:8333",
        bucket="factory-agent-test",
        access_key="test-key",
        secret_key="test-secret",
        session=_FakeSession(gateway),
    )


def _result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("fr008_payroll_ranking"),
        column_names=("uid", "uname", "gross"),
        rows=(("01001", "模拟", Decimal("21.65")),),
        column_types={"gross": "money"},
    )


@pytest.mark.asyncio
async def test_put_then_get_round_trips_bytes_and_owner_binding() -> None:
    gateway = _FakeGateway()
    store = _store(gateway)

    await store.put(_artifact("art-1"))

    assert sorted(gateway.objects) == ["exports/art-1.json", "exports/art-1.xlsx"]
    # Bytes land before the sidecar: a readable id always has bytes behind it.
    assert gateway.put_calls == ["exports/art-1.xlsx", "exports/art-1.json"]
    assert await store.get("art-1") == _artifact("art-1")


@pytest.mark.asyncio
async def test_missing_object_is_an_ordinary_miss() -> None:
    assert await _store(_FakeGateway()).get("nope") is None


@pytest.mark.asyncio
async def test_missing_bytes_with_a_readable_sidecar_is_a_miss() -> None:
    gateway = _FakeGateway()
    store = _store(gateway)
    await store.put(_artifact("art-1"))

    del gateway.objects["exports/art-1.xlsx"]

    assert await store.get("art-1") is None


@pytest.mark.asyncio
async def test_corrupt_sidecar_is_a_miss() -> None:
    gateway = _FakeGateway()
    gateway.objects["exports/art-1.xlsx"] = b"PK\x03\x04"
    gateway.objects["exports/art-1.json"] = b"{not json"

    assert await _store(gateway).get("art-1") is None


@pytest.mark.asyncio
async def test_write_failure_raises_structured_unavailable() -> None:
    gateway = _FakeGateway()
    gateway.put_failures = 1

    with pytest.raises(ExportError) as caught:
        await _store(gateway).put(_artifact("art-1"))

    assert caught.value.code == ErrorCatalog.UNAVAILABLE
    # Nothing readable was left behind, so no download id is ever handed out.
    assert gateway.objects == {}


@pytest.mark.asyncio
async def test_delete_failure_is_swallowed_but_success_removes_both_objects() -> None:
    gateway = _FakeGateway()
    store = _store(gateway)
    await store.put(_artifact("art-1"))

    gateway.delete_failure = True
    await store.delete("art-1")  # best effort: never raises
    assert gateway.objects != {}

    gateway.delete_failure = False
    await store.delete("art-1")
    assert gateway.objects == {}


@pytest.mark.asyncio
async def test_export_service_serves_downloads_from_the_object_store() -> None:
    """The service is backend-agnostic: a cold instance still serves the id."""
    store = _store(_FakeGateway())
    service = ExportService(store=store, clock=lambda: _NOW, new_id=lambda: "art-1")

    outcome = await service.export(
        owner=_OWNER,
        interaction_id="it-1",
        capability_id=CapabilityId("fr008_payroll_ranking"),
        role="manager",
        function="FR-008",
        time_range_label="2026-08-01_2026-08-31",
        result=_result(),
    )

    assert outcome.artifact_id == "art-1"
    # A restart clears the read cache; the object store keeps serving.
    cold = ExportService(store=store, clock=lambda: _NOW)
    content = await cold.fetch(_OWNER, "art-1")
    assert content is not None
    assert content.content[:2] == b"PK"
    assert content.filename.endswith(".xlsx")
