"""Concrete artifact exporter wiring the renderer, object store, and repository.

This module lives at the package root because it composes the ``export``
renderer, the artifact object store, and the persistence repository — a
combination no single governed subpackage may own. The session/application
layers depend only on the ``ArtifactExporter`` port.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from factory_agent.domain import CapabilityId
from factory_agent.domain.errors import InvalidRequestError, UpstreamUnavailableError
from factory_agent.execution.kernel import render_table_from_run_result
from factory_agent.export.artifacts import FilesystemArtifactStore, S3ArtifactStore
from factory_agent.export.sanitize import build_export_filename
from factory_agent.export.xlsx import render_xlsx
from factory_agent.ports.artifacts import (
    ArtifactRecord,
    ArtifactRepository,
    ErrorCatalog,
    ExportError,
    ExportOutcome,
)
from factory_agent.ports.contracts import ArtifactStore, Clock
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportService:
    """Renders a ``CapabilityRunResult`` into XLSX and records its metadata."""

    def __init__(
        self,
        store: ArtifactStore,
        repository: ArtifactRepository,
        *,
        presign_expires_seconds: int = 900,
        cleanup_after_days: int = 90,
        secret_prefix: str = "factory-agent",
        new_id: Callable[[], str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._repository = repository
        self._presign_expires_seconds = presign_expires_seconds
        self._cleanup_after_days = cleanup_after_days
        self._secret_prefix = secret_prefix
        self._new_id = new_id or (lambda: uuid4().hex)
        self._clock = clock

    async def export(
        self,
        *,
        owner: InteractionOwner,
        interaction_id: str,
        capability_id: CapabilityId,
        role: str,
        function: str,
        time_range_label: str,
        result: CapabilityRunResult,
    ) -> ExportOutcome:
        render_table = render_table_from_run_result(result)
        generated_at = _timestamp_label(self._clock)
        filename = build_export_filename(role, function, time_range_label, generated_at)
        try:
            content = render_xlsx(render_table)
        except Exception as error:  # noqa: BLE001 - renderer failures are bounded
            raise ExportError(ErrorCatalog.INVALID, "renderer could not produce XLSX") from error
        if not content:
            raise ExportError(ErrorCatalog.INVALID, "renderer produced empty content")

        artifact_id = self._new_id()
        object_key = f"{self._secret_prefix}/{artifact_id}"
        try:
            await self._store.put(artifact_id, content, _XLSX_CONTENT_TYPE)
        except (InvalidRequestError, UpstreamUnavailableError) as error:
            raise ExportError(ErrorCatalog.UPLOAD_FAILED, "artifact upload failed") from error

        created_at = self._now()
        record = ArtifactRecord(
            artifact_id=artifact_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            interaction_id=interaction_id,
            capability_id=capability_id,
            object_key=object_key,
            filename=filename,
            content_type=_XLSX_CONTENT_TYPE,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=created_at,
            expires_at=created_at + timedelta(days=self._cleanup_after_days),
        )
        await self._repository.save(record)
        return ExportOutcome(
            artifact_id=artifact_id,
            filename=filename,
            size_bytes=len(content),
            sha256=record.sha256,
            expires_at=record.expires_at,
        )

    async def presign(self, owner: InteractionOwner, artifact_id: str) -> str:
        record = await self._get_owned(owner, artifact_id)
        if record is None:
            raise ExportError(ErrorCatalog.NOT_FOUND, "artifact not found")
        try:
            return await self._store.presign(artifact_id, self._presign_expires_seconds)
        except (InvalidRequestError, UpstreamUnavailableError) as error:
            raise ExportError(ErrorCatalog.UPLOAD_FAILED, "artifact download failed") from error

    async def cleanup(self, now: datetime) -> int:
        expired = await self._repository.list_expired(now)
        for record in expired:
            try:
                await self._store.delete(record.artifact_id)
            except (InvalidRequestError, UpstreamUnavailableError):
                continue
            await self._repository.delete(record.artifact_id)
        return len(expired)

    async def _get_owned(self, owner: InteractionOwner, artifact_id: str) -> ArtifactRecord | None:
        return await self._repository.get(owner, artifact_id)

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        from datetime import timezone

        return datetime.now(timezone.utc)


def _timestamp_label(clock: Clock | None) -> str:
    now = clock.now() if clock is not None else datetime.now()
    return now.strftime("%Y%m%d%H%M%S")


__all__ = [
    "ExportError",
    "ExportService",
    "FilesystemArtifactStore",
    "S3ArtifactStore",
]
