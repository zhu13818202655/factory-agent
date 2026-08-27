"""Retention routine verification (Story 8).

Checks the 3-month artifact cleanup and the download re-authorization path:
cleanup removes only expired artifacts (never another tenant's live data) and a
download is ownership-filtered — a different user cannot read an old artifact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.export.artifacts import FilesystemArtifactStore
from factory_agent.export_service import ExportService
from factory_agent.ports.artifacts import ArtifactRecord, ExportError
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner
from tests.unit.test_export_service import FakeArtifactRepository

_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
_TENANT = TenantId("APPKEY-A")
_USER = UserId("01001")


def _result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("fr003_personal_wage_detail"),
        column_names=("rq", "worktype", "sl", "je"),
        rows=(("2026-08-06", "WT01", Decimal("4"), Decimal("4.00")),),
        totals={"je": Decimal("4.00")},
        source_operations=("GongziMxQuery",),
        column_types={"rq": "date", "sl": "quantity", "je": "money"},
    )


class FixedClock:
    def now(self) -> datetime:
        return _TIME


def _owner() -> InteractionOwner:
    return InteractionOwner(tenant_id=_TENANT, user_id=_USER)


async def _export(
    service: ExportService,
    owner: InteractionOwner,
    interaction_id: str,
) -> str:
    outcome = await service.export(
        owner=owner,
        interaction_id=interaction_id,
        capability_id=CapabilityId("fr003_personal_wage_detail"),
        role="员工",
        function="明细",
        time_range_label="range",
        result=_result(),
    )
    return outcome.artifact_id


def _expire(repo: FakeArtifactRepository, artifact_id: str) -> None:
    record = repo.records[artifact_id]
    repo.records[artifact_id] = ArtifactRecord(
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


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired_artifacts(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(store, repo, clock=FixedClock(), cleanup_after_days=90)

    expired_id = await _export(service, _owner(), "i-1")
    live_id = await _export(service, _owner(), "i-2")
    _expire(repo, expired_id)

    removed = await service.cleanup(_TIME)

    assert removed == 1
    assert set(repo.records) == {live_id}
    # The live artifact's content is untouched.
    assert await store.get(live_id) is not None


@pytest.mark.asyncio
async def test_cleanup_never_deletes_another_tenants_live_artifact(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(store, repo, clock=FixedClock(), cleanup_after_days=90)

    # A live artifact owned by a different tenant must survive the routine.
    other_owner = InteractionOwner(tenant_id=TenantId("APPKEY-B"), user_id=UserId("02001"))
    other_id = await _export(service, other_owner, "i-other")

    removed = await service.cleanup(_TIME)

    assert removed == 0
    assert other_id in repo.records


@pytest.mark.asyncio
async def test_download_requires_reauthorization_across_users(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repo = FakeArtifactRepository()
    service = ExportService(store, repo, clock=FixedClock(), cleanup_after_days=90)

    artifact_id = await _export(service, _owner(), "i-1")

    # The owning user can presign a short-lived link.
    assert await service.presign(_owner(), artifact_id)
    # A different user in the same tenant cannot: indistinguishable from missing.
    other = InteractionOwner(tenant_id=_TENANT, user_id=UserId("01002"))
    with pytest.raises(ExportError):
        await service.presign(other, artifact_id)
