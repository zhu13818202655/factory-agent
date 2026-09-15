"""Export artifact backends: local directory, S3 gateway, and backend selection.

The S3 tests use an in-memory fake session so the store's error mapping is
exercised without a live object store: a missing object stays an ordinary miss
and a write that never landed fails the export instead of handing out a dead
download link.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[reportMissingTypeStubs]
from pydantic import SecretStr
from usage_admin.config import UsageAdminSettings
from usage_admin.container import (
    build_container,
    build_export_file_store,
)
from usage_admin.export_store import (
    ErrorCode,
    ExportStoreError,
    InMemoryExportFileStore,
    LocalExportFileStore,
    S3ExportFileStore,
    content_type_for,
)
from usage_admin.exports import ExportService, sign_download
from usage_admin.ops import OpsService
from usage_admin.platform import PlatformRole, PlatformScope
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SECRET = "test-secret"
KEY = "exports/export-0.csv"


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
        self.put_calls: list[tuple[str, str]] = []


class _FakeClient:
    def __init__(self, gateway: _FakeGateway) -> None:
        self._gateway = gateway

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 - the S3 API's own keyword names
        Key: str,  # noqa: N803
        Body: bytes,  # noqa: N803
        ContentType: str,  # noqa: N803
    ) -> None:
        self._gateway.put_calls.append((Key, ContentType))
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


def _s3_store(gateway: _FakeGateway) -> S3ExportFileStore:
    return S3ExportFileStore(
        endpoint_url="http://127.0.0.1:8333",
        bucket="usage-admin-test",
        access_key="test-key",
        secret_key="test-secret",
        session=_FakeSession(gateway),
    )


def _analyst() -> PlatformScope:
    return PlatformScope("ops-1", PlatformRole.ANALYST, frozenset())


def test_content_type_for_maps_known_extensions() -> None:
    assert content_type_for("exports/x.csv") == "text/csv"
    assert content_type_for("exports/x.xlsx").startswith("application/vnd.openxmlformats")
    assert content_type_for("exports/x.bin") == "application/octet-stream"


@pytest.mark.asyncio
async def test_local_store_round_trips_and_misses_cleanly(tmp_path: Path) -> None:
    store = LocalExportFileStore(tmp_path / "exports")

    await store.put(KEY, b"a,b\n1,2\n")

    assert await store.get(KEY) == b"a,b\n1,2\n"
    assert await store.get("exports/nope.csv") is None

    await store.delete(KEY)
    assert await store.get(KEY) is None


@pytest.mark.asyncio
async def test_local_store_survives_a_new_instance(tmp_path: Path) -> None:
    """Persistence across restarts is the whole point of the backend."""
    await LocalExportFileStore(tmp_path / "exports").put(KEY, b"payload")

    assert await LocalExportFileStore(tmp_path / "exports").get(KEY) == b"payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["../escape.csv", "/etc/passwd", "exports/../../escape.csv"])
async def test_local_store_rejects_a_traversing_key(tmp_path: Path, key: str) -> None:
    store = LocalExportFileStore(tmp_path / "exports")

    with pytest.raises(ExportStoreError) as caught:
        await store.put(key, b"x")

    assert caught.value.code == ErrorCode.INVALID_KEY


@pytest.mark.asyncio
async def test_local_store_write_failure_raises_structured_unavailable(tmp_path: Path) -> None:
    # A regular file where the store directory should be: mkdir cannot succeed.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    with pytest.raises(ExportStoreError) as caught:
        await LocalExportFileStore(blocked / "exports").put(KEY, b"x")

    assert caught.value.code == ErrorCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_local_store_delete_failure_is_swallowed(tmp_path: Path) -> None:
    store = LocalExportFileStore(tmp_path / "exports")
    # A directory where the artifact file should be: unlink fails, deletion is
    # best effort and must not raise.
    (tmp_path / "exports" / "exports" / "export-0.csv").mkdir(parents=True)

    await store.delete(KEY)


@pytest.mark.asyncio
async def test_s3_store_round_trips_bytes_and_content_type() -> None:
    gateway = _FakeGateway()
    store = _s3_store(gateway)

    await store.put(KEY, b"a,b\n")

    assert gateway.objects == {KEY: b"a,b\n"}
    assert gateway.put_calls == [(KEY, "text/csv")]
    assert await store.get(KEY) == b"a,b\n"


@pytest.mark.asyncio
async def test_s3_missing_object_is_an_ordinary_miss() -> None:
    assert await _s3_store(_FakeGateway()).get("exports/nope.csv") is None


@pytest.mark.asyncio
async def test_s3_write_failure_raises_structured_unavailable() -> None:
    gateway = _FakeGateway()
    gateway.put_failures = 1

    with pytest.raises(ExportStoreError) as caught:
        await _s3_store(gateway).put(KEY, b"x")

    assert caught.value.code == ErrorCode.UNAVAILABLE
    assert gateway.objects == {}


@pytest.mark.asyncio
async def test_s3_delete_failure_is_swallowed_but_success_removes_the_object() -> None:
    gateway = _FakeGateway()
    store = _s3_store(gateway)
    await store.put(KEY, b"x")

    gateway.delete_failure = True
    await store.delete(KEY)
    assert gateway.objects != {}

    gateway.delete_failure = False
    await store.delete(KEY)
    assert gateway.objects == {}


def test_backend_selection_prefers_s3_then_local_then_memory() -> None:
    s3 = build_export_file_store(
        UsageAdminSettings(
            s3_endpoint_url="http://seaweedfs:8333",
            s3_bucket="usage-admin-exports",
            s3_access_key=SecretStr("k"),
            s3_secret_key=SecretStr("s"),
        )
    )
    assert isinstance(s3, S3ExportFileStore)

    local = build_export_file_store(UsageAdminSettings(export_store_dir="/tmp/ua-exports"))
    assert isinstance(local, LocalExportFileStore)

    # S3 wins when both are configured.
    both = build_export_file_store(
        UsageAdminSettings(s3_endpoint_url="http://seaweedfs:8333", export_store_dir="/tmp/ua")
    )
    assert isinstance(both, S3ExportFileStore)

    assert isinstance(build_export_file_store(UsageAdminSettings()), InMemoryExportFileStore)


def test_container_uses_the_configured_backend(tmp_path: Path) -> None:
    container = build_container(UsageAdminSettings(export_store_dir=str(tmp_path / "exports")))

    assert isinstance(container.files, LocalExportFileStore)


@pytest.mark.asyncio
async def test_a_restart_still_serves_the_signed_download(tmp_path: Path) -> None:
    """Create on one instance, download on a fresh one: the token still works.

    The usage store stands in for PostgreSQL (which survives a restart); only
    the file backend is rebuilt, which is the part the export backend owns.
    """
    store_dir = tmp_path / "exports"
    usage = InMemoryUsageStore()

    def service() -> ExportService:
        return ExportService(
            usage,
            OpsService(usage, clock=lambda: NOW),
            LocalExportFileStore(store_dir),
            clock=lambda: NOW,
            new_id=lambda: "export-0",
            signing_secret=SECRET,
            download_base_url="http://usage-admin.test",
        )

    created = await service().create_export(
        _analyst(), start=START, end=END, format="csv", metrics=()
    )

    assert created.expires_at is not None
    token = sign_download(SECRET, created.export_id, created.expires_at)
    served = await service().download(token)

    assert served is not None
    content, fmt = served
    assert fmt == "csv"
    assert content.startswith(b"users,")


@pytest.mark.asyncio
async def test_an_unsigned_or_expired_link_is_still_refused(tmp_path: Path) -> None:
    """Persisting bytes must not widen who can download them."""
    store_dir = tmp_path / "exports"
    usage = InMemoryUsageStore()

    def service() -> ExportService:
        return ExportService(
            usage,
            OpsService(usage, clock=lambda: NOW),
            LocalExportFileStore(store_dir),
            clock=lambda: NOW,
            new_id=lambda: "export-0",
            signing_secret=SECRET,
            download_base_url="http://usage-admin.test",
        )

    await service().create_export(_analyst(), start=START, end=END, format="csv", metrics=())

    assert await service().download("garbage") is None
    expired = sign_download(SECRET, "export-0", NOW - timedelta(seconds=1))
    assert await service().download(expired) is None
    forged = sign_download("wrong-secret", "export-0", NOW + timedelta(minutes=5))
    assert await service().download(forged) is None
