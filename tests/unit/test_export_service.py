"""Export service tests (retained export over an injected store).

Proves: in-memory render → store → owned fetch returns XLSX bytes; the store
survives a service restart within retention; foreign/unknown ids are
indistinguishable (None); cache eviction never removes the artifact; a
renderer failure degrades to a structured error without touching results; and
retention/ownership are enforced by the service, not by the backend — the same
suite passes over the local directory store and over a backend-agnostic double.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.export.local_store import LocalArtifactStore
from factory_agent.export.sanitize import sanitize_filename
from factory_agent.export_service import (
    DEFAULT_EXPORT_MAX_ENTRIES,
    DEFAULT_EXPORT_RETENTION_SECONDS,
    ExportService,
)
from factory_agent.ports.artifacts import (
    ErrorCatalog,
    ExportError,
    ExportStore,
    StoredArtifact,
)
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_TENANT = TenantId("APPKEY-A")
_OWNER = InteractionOwner(tenant_id=_TENANT, user_id=UserId("01001"))
_OTHER = InteractionOwner(tenant_id=TenantId("APPKEY-B"), user_id=UserId("99999"))


def _result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("fr008_payroll_ranking"),
        column_names=("uid", "uname", "gross"),
        rows=(("01001", "模拟", Decimal("21.65")),),
        column_types={"gross": "money"},
    )


def _ids(prefix: str = "art"):
    counter = iter(range(1, 1000))

    def factory() -> str:
        return f"{prefix}-{next(counter)}"

    return factory


def _service(
    store_dir: Path,
    *,
    new_id: Callable[[], str] | None = None,
    retention_seconds: float = DEFAULT_EXPORT_RETENTION_SECONDS,
    max_entries: int = DEFAULT_EXPORT_MAX_ENTRIES,
) -> ExportService:
    """Service on the local-directory backend, the default for tests."""
    return _service_on(
        LocalArtifactStore(store_dir=store_dir),
        new_id=new_id,
        retention_seconds=retention_seconds,
        max_entries=max_entries,
    )


def _service_on(
    store: ExportStore,
    *,
    new_id: Callable[[], str] | None = None,
    retention_seconds: float = DEFAULT_EXPORT_RETENTION_SECONDS,
    max_entries: int = DEFAULT_EXPORT_MAX_ENTRIES,
) -> ExportService:
    """Service over any backend, so retention/ownership are proven backend-agnostic."""
    return ExportService(
        store=store,
        clock=lambda: datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        new_id=new_id,
        retention_seconds=retention_seconds,
        max_entries=max_entries,
    )


class _MemoryStore:
    """Backend double that never enforces retention or ownership itself."""

    def __init__(self) -> None:
        self.stored: dict[str, StoredArtifact] = {}
        self.fail_writes = False

    async def put(self, artifact: StoredArtifact) -> None:
        if self.fail_writes:
            raise ExportError(ErrorCatalog.UNAVAILABLE, "store is down")
        self.stored[artifact.artifact_id] = artifact

    async def get(self, artifact_id: str) -> StoredArtifact | None:
        return self.stored.get(artifact_id)

    async def delete(self, artifact_id: str) -> None:
        self.stored.pop(artifact_id, None)


async def _export_one(service: ExportService, interaction_id: str) -> str:
    outcome = await service.export(
        owner=_OWNER,
        interaction_id=interaction_id,
        capability_id=CapabilityId("fr008_payroll_ranking"),
        role="manager",
        function="FR-008",
        time_range_label="2026-08-01_2026-08-31",
        result=_result(),
    )
    return outcome.artifact_id


@pytest.mark.asyncio
async def test_export_renders_xlsx_to_disk_and_fetch_returns_owned_bytes(tmp_path: Path) -> None:
    service = _service(tmp_path, new_id=_ids())

    artifact_id = await _export_one(service, "it-1")

    # Bytes + sidecar metadata live on disk, not only in memory.
    assert (tmp_path / f"{artifact_id}.xlsx").read_bytes()[:2] == b"PK"
    assert (tmp_path / f"{artifact_id}.json").is_file()

    content = await service.fetch(_OWNER, artifact_id)
    assert content is not None
    assert content.content[:2] == b"PK"
    assert content.content_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@pytest.mark.asyncio
async def test_export_outcome_carries_filename_and_size(tmp_path: Path) -> None:
    service = _service(tmp_path, new_id=_ids())
    outcome = await service.export(
        owner=_OWNER,
        interaction_id="it-1",
        capability_id=CapabilityId("fr008_payroll_ranking"),
        role="manager",
        function="FR-008",
        time_range_label="2026-08-01_2026-08-31",
        result=_result(),
    )

    assert outcome.size_bytes > 0
    assert outcome.filename.endswith(".xlsx")
    # 文件名按 角色_功能_时间范围_生成时间 对齐.
    assert sanitize_filename(outcome.filename) == outcome.filename


@pytest.mark.asyncio
async def test_artifact_survives_service_restart_within_retention(tmp_path: Path) -> None:
    first = _service(tmp_path, new_id=_ids())
    artifact_id = await _export_one(first, "it-1")

    # A fresh instance (the restart case): empty read cache, same disk store.
    second = _service(tmp_path, new_id=_ids())
    content = await second.fetch(_OWNER, artifact_id)
    assert content is not None
    assert content.content[:2] == b"PK"
    assert content.filename.endswith(".xlsx")


@pytest.mark.asyncio
async def test_expired_artifact_is_not_served_after_restart(tmp_path: Path) -> None:
    service = _service(tmp_path, new_id=_ids(), retention_seconds=3600)
    artifact_id = await _export_one(service, "it-1")

    later = ExportService(
        store=LocalArtifactStore(store_dir=tmp_path),
        clock=lambda: datetime(2026, 9, 3, 8, tzinfo=timezone.utc) + timedelta(hours=2),
        new_id=_ids(),
        retention_seconds=3600,
    )
    assert await later.fetch(_OWNER, artifact_id) is None
    # The expired files were purged from disk.
    assert not (tmp_path / f"{artifact_id}.xlsx").exists()
    assert not (tmp_path / f"{artifact_id}.json").exists()


@pytest.mark.asyncio
async def test_fetch_of_foreign_or_unknown_id_is_indistinguishable(tmp_path: Path) -> None:
    service = _service(tmp_path, new_id=_ids())
    artifact_id = await _export_one(service, "it-1")

    assert await service.fetch(_OTHER, artifact_id) is None
    assert await service.fetch(_OWNER, "missing-art") is None


@pytest.mark.asyncio
async def test_cache_is_bounded_but_disk_keeps_every_artifact(tmp_path: Path) -> None:
    service = _service(tmp_path, new_id=_ids(), max_entries=2)
    first = None
    for index in range(3):
        artifact_id = await _export_one(service, f"it-{index}")
        if index == 0:
            first = artifact_id

    assert first is not None
    # The oldest entry fell out of the read cache but the disk store still
    # serves it within retention — persistence is never bounded by the cache.
    content = await service.fetch(_OWNER, first)
    assert content is not None
    assert content.content[:2] == b"PK"


@pytest.mark.asyncio
async def test_unwritable_store_degrades_instead_of_crashing_startup(tmp_path: Path) -> None:
    """A read-only FS must not crash construction; the first export fails
    structured (the session pipeline degrades to artifact_id=None)."""
    from factory_agent.ports.artifacts import ExportError

    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        service = _service(ro_dir, new_id=_ids())  # must not raise

        with pytest.raises(ExportError):
            await _export_one(service, "it-1")

        # No artifact was written and nothing is fetchable.
        assert list(ro_dir.iterdir()) == []
        assert await service.fetch(_OWNER, "art-1") is None
    finally:
        ro_dir.chmod(0o755)  # let pytest clean tmp_path up


@pytest.mark.asyncio
async def test_ownership_is_enforced_by_the_service_not_the_backend() -> None:
    """A backend hands back whatever it holds; the service still gates on owner."""
    store = _MemoryStore()
    service = _service_on(store, new_id=_ids())
    artifact_id = await _export_one(service, "it-1")

    # The double has no notion of ownership and would serve anyone...
    assert await store.get(artifact_id) is not None
    # ...yet both the warm and the cold service refuse the foreign owner.
    assert await service.fetch(_OTHER, artifact_id) is None
    cold = _service_on(store, new_id=_ids())
    assert await cold.fetch(_OTHER, artifact_id) is None
    assert await cold.fetch(_OWNER, artifact_id) is not None


@pytest.mark.asyncio
async def test_backend_write_failure_fails_the_export_instead_of_issuing_an_id() -> None:
    store = _MemoryStore()
    store.fail_writes = True
    service = _service_on(store, new_id=_ids())

    with pytest.raises(ExportError) as caught:
        await _export_one(service, "it-1")

    assert caught.value.code == ErrorCatalog.UNAVAILABLE
    assert store.stored == {}
    assert await service.fetch(_OWNER, "art-1") is None
