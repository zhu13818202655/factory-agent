"""``S3ArtifactStore`` against a real S3-compatible gateway (SeaweedFS).

The offline proof of the store's error mapping lives in
``tests/unit/export/test_s3_store.py`` (fake session). This suite exercises the
same store against a live gateway so the object layout, the sidecar round-trip,
the S3 client configuration, and credential handling are proven against a real
implementation rather than a double.

Enable it by pointing the four ``FACTORY_AGENT_TEST_S3_*`` variables at a
disposable bucket (see ``deploy/compose/README.md``); without them the suite
skips. Every object it writes is removed in teardown, and it never touches a
production bucket.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.export.s3_store import S3ArtifactStore
from factory_agent.export_service import ExportService
from factory_agent.ports.artifacts import ExportError, StoredArtifact
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

ENDPOINT_URL = os.environ.get("FACTORY_AGENT_TEST_S3_ENDPOINT_URL")
BUCKET = os.environ.get("FACTORY_AGENT_TEST_S3_BUCKET")
ACCESS_KEY = os.environ.get("FACTORY_AGENT_TEST_S3_ACCESS_KEY")
SECRET_KEY = os.environ.get("FACTORY_AGENT_TEST_S3_SECRET_KEY")

pytestmark = [
    pytest.mark.skipif(
        not all((ENDPOINT_URL, BUCKET, ACCESS_KEY, SECRET_KEY)),
        reason="set FACTORY_AGENT_TEST_S3_* to a disposable bucket to run these tests",
    ),
    pytest.mark.asyncio,
]

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
OWNER = InteractionOwner(tenant_id=TenantId("APPKEY-TEST"), user_id=UserId("01001"))
OTHER = InteractionOwner(tenant_id=TenantId("APPKEY-TEST"), user_id=UserId("09999"))


def _store(prefix: str) -> S3ArtifactStore:
    assert ENDPOINT_URL is not None and BUCKET is not None
    assert ACCESS_KEY is not None and SECRET_KEY is not None
    return S3ArtifactStore(
        endpoint_url=ENDPOINT_URL,
        bucket=BUCKET,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        # One run-local prefix keeps concurrent runs and any pre-existing
        # objects out of each other's way, and makes teardown exact.
        prefix=prefix,
    )


def _result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("fr008_payroll_ranking"),
        column_names=("uid", "uname", "gross"),
        rows=(("01001", "模拟", Decimal("21.65")),),
        column_types={"gross": "money"},
    )


@pytest.mark.asyncio
async def test_put_get_delete_round_trip_against_the_gateway() -> None:
    store = _store("it-s3-roundtrip/")
    artifact = StoredArtifact(
        artifact_id="it-artifact-1",
        tenant_id=str(OWNER.tenant_id),
        user_id=str(OWNER.user_id),
        filename="管理_FR-008_2026-08-01_2026-08-31_20260914080000.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=b"PK\x03\x04round-trip",
        expires_at=NOW + timedelta(days=7),
    )
    try:
        await store.put(artifact)
        assert await store.get("it-artifact-1") == artifact
        assert await store.get("does-not-exist") is None

        await store.delete("it-artifact-1")
        assert await store.get("it-artifact-1") is None
    finally:
        await store.delete("it-artifact-1")


@pytest.mark.asyncio
async def test_wrong_credentials_are_rejected_by_the_gateway() -> None:
    """A misconfigured endpoint must fail the export, never silently succeed."""
    assert ENDPOINT_URL is not None and BUCKET is not None
    wrong = S3ArtifactStore(
        endpoint_url=ENDPOINT_URL,
        bucket=BUCKET,
        access_key="definitely-not-the-key",
        secret_key="definitely-not-the-secret",
        prefix="it-s3-denied/",
    )

    with pytest.raises(ExportError):
        await wrong.put(
            StoredArtifact(
                artifact_id="it-artifact-denied",
                tenant_id=str(OWNER.tenant_id),
                user_id=str(OWNER.user_id),
                filename="denied.xlsx",
                content_type="application/octet-stream",
                content=b"PK",
                expires_at=NOW + timedelta(days=1),
            )
        )


@pytest.mark.asyncio
async def test_export_service_download_survives_a_restart() -> None:
    store = _store("it-s3-service/")
    service = ExportService(store=store, clock=lambda: NOW, new_id=lambda: "it-artifact-2")
    try:
        outcome = await service.export(
            owner=OWNER,
            interaction_id="it-1",
            capability_id=CapabilityId("fr008_payroll_ranking"),
            role="manager",
            function="FR-008",
            time_range_label="2026-08-01_2026-08-31",
            result=_result(),
        )

        # A fresh process (empty read cache, same bucket) still serves the id.
        cold = ExportService(store=store, clock=lambda: NOW)
        content = await cold.fetch(OWNER, outcome.artifact_id)
        assert content is not None
        assert content.content[:2] == b"PK"
        # Ownership survives the restart because the binding is persisted.
        assert await cold.fetch(OTHER, outcome.artifact_id) is None
    finally:
        await store.delete("it-artifact-2")
