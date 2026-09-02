"""Export service tests: render, store, metadata, presign, cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.domain.errors import UpstreamUnavailableError
from factory_agent.export.artifacts import FilesystemArtifactStore
from factory_agent.export_service import ExportService
from factory_agent.ports.artifacts import ArtifactRecord, ExportError
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_TENANT = TenantId("APPKEY-A")
_USER = UserId("01001")
_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class FakeArtifactRepository:
    """In-memory artifact metadata repository."""

    def __init__(self) -> None:
        self.records: dict[str, ArtifactRecord] = {}

    async def save(self, record: ArtifactRecord) -> None:
        self.records[record.artifact_id] = record

    async def get(self, owner: InteractionOwner, artifact_id: str) -> ArtifactRecord | None:
        record = self.records.get(artifact_id)
        if record is None:
            return None
        if record.tenant_id != owner.tenant_id or record.user_id != owner.user_id:
            return None
        return record

    async def delete(self, artifact_id: str) -> None:
        self.records.pop(artifact_id, None)

    async def list_expired(self, now: datetime) -> tuple[ArtifactRecord, ...]:
        return tuple(r for r in self.records.values() if r.expires_at <= now)


class FailingStore(FilesystemArtifactStore):
    async def put(self, artifact_id: str, content: bytes, content_type: str) -> None:
        raise UpstreamUnavailableError("upload unavailable")


def _owner() -> InteractionOwner:
    return InteractionOwner(tenant_id=_TENANT, user_id=_USER)


def _result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("fr003_personal_wage_detail"),
        column_names=("rq", "worktype", "sl", "je"),
        rows=(("2026-08-06", "WT01", Decimal("4"), Decimal("4.00")),),
        totals={"je": Decimal("4.00")},
        source_operations=("GongziMxQuery",),
        column_types={"rq": "date", "sl": "quantity", "je": "money"},
    )


def _clock() -> Any:
    return _FixedClock()


class _FixedClock:
    def now(self) -> datetime:
        return _TIME


@pytest.mark.asyncio
async def test_export_renders_stores_and_records_metadata(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(
        store, repo, clock=_clock(), cleanup_after_days=90, presign_expires_seconds=900
    )
    outcome = await service.export(
        owner=_owner(),
        interaction_id="i-1",
        capability_id=CapabilityId("fr003_personal_wage_detail"),
        role="员工",
        function="个人工资明细",
        time_range_label="2026-07-01_2026-08-31",
        result=_result(),
    )

    assert outcome.filename == "员工_个人工资明细_2026-07-01_2026-08-31_20260826120000.xlsx"
    assert outcome.size_bytes > 0
    assert outcome.artifact_id in repo.records
    stored = await store.get(outcome.artifact_id)
    assert stored.startswith(b"PK") and len(stored) == outcome.size_bytes
    assert repo.records[outcome.artifact_id].tenant_id == _TENANT
    assert repo.records[outcome.artifact_id].expires_at == _TIME + timedelta(days=90)
    # No employee ID or amount in the object key.
    assert "01001" not in repo.records[outcome.artifact_id].object_key
    assert "21.65" not in repo.records[outcome.artifact_id].object_key


@pytest.mark.asyncio
async def test_presign_requires_tenancy_ownership(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(store, repo, clock=_clock())
    outcome = await service.export(
        owner=_owner(),
        interaction_id="i-1",
        capability_id=CapabilityId("fr003_personal_wage_detail"),
        role="员工",
        function="明细",
        time_range_label="range",
        result=_result(),
    )

    url = await service.presign(_owner(), outcome.artifact_id)
    assert "expires=900" in url

    foreign = InteractionOwner(tenant_id=TenantId("APPKEY-B"), user_id=UserId("02001"))
    with pytest.raises(ExportError):
        await service.presign(foreign, outcome.artifact_id)


@pytest.mark.asyncio
async def test_upload_failure_produces_no_downloadable_artifact(tmp_path: Path) -> None:
    store = FailingStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(store, repo, clock=_clock())
    with pytest.raises(ExportError):
        await service.export(
            owner=_owner(),
            interaction_id="i-1",
            capability_id=CapabilityId("fr003_personal_wage_detail"),
            role="员工",
            function="明细",
            time_range_label="range",
            result=_result(),
        )
    assert repo.records == {}


@pytest.mark.asyncio
async def test_cleanup_deletes_expired_artifacts(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(store, repo, clock=_clock(), cleanup_after_days=90)

    outcome = await service.export(
        owner=_owner(),
        interaction_id="i-1",
        capability_id=CapabilityId("fr003_personal_wage_detail"),
        role="员工",
        function="明细",
        time_range_label="range",
        result=_result(),
    )
    # Force one artifact to be expired.
    record = repo.records[outcome.artifact_id]
    repo.records[outcome.artifact_id] = ArtifactRecord(
        artifact_id=record.artifact_id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        interaction_id=record.interaction_id,
        capability_id=record.capability_id,
        object_key=record.object_key,
        filename=record.filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        created_at=record.created_at,
        expires_at=_TIME - timedelta(days=1),
    )

    removed = await service.cleanup(_TIME)
    assert removed == 1
    assert repo.records == {}
    with pytest.raises(Exception):
        await store.get(outcome.artifact_id)
