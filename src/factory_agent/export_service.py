"""Instant export with retained storage (即时生成、直接下载、落盘保留).

Renders a ``CapabilityRunResult`` into XLSX fully in memory, then hands the
bytes plus the owner binding to an ``ExportStore`` backend — the local
directory by default, an S3-compatible object store when one is configured.
Downloads therefore survive process restarts within the retention window, and
expired artifacts are purged lazily. Every fetch re-checks the persisted owner
binding (tenant/user); a missing, expired, or foreign export id stays an
indistinguishable 404 — regeneration goes through history/favorite re-ask.

Retention and ownership live here, not in the backend, so both backends behave
identically. The in-memory buffer is only a read cache in front of the store;
its entry cap bounds memory, never availability. This module lives at the
package root because it composes the ``export`` renderer and an artifact store;
the session/application layers depend only on the ``ArtifactExporter`` port.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from factory_agent.domain import CapabilityId
from factory_agent.execution.kernel import render_table_from_run_result
from factory_agent.export.sanitize import build_export_filename
from factory_agent.export.xlsx import render_xlsx
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.artifacts import (
    ErrorCatalog,
    ExportContent,
    ExportError,
    ExportOutcome,
    ExportStore,
    StoredArtifact,
)
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_LOGGER = get_logger("factory_agent.export_service")

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: Default retention before a generated export is purged from the store (seconds).
DEFAULT_EXPORT_RETENTION_SECONDS = 90 * 86400
#: Hard cap on the in-memory read cache so a busy tenant cannot exhaust memory.
DEFAULT_EXPORT_MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class _Entry:
    tenant_id: str
    user_id: str
    filename: str
    content_type: str
    content: bytes
    expires_at: datetime


class ExportService:
    """Renders a ``CapabilityRunResult`` into retained, downloadable XLSX."""

    def __init__(
        self,
        *,
        store: ExportStore,
        clock: Callable[[], datetime] | None = None,
        new_id: Callable[[], str] | None = None,
        retention_seconds: float = DEFAULT_EXPORT_RETENTION_SECONDS,
        max_entries: int = DEFAULT_EXPORT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._new_id = new_id or (lambda: uuid4().hex)
        self._store = store
        self._retention_seconds = retention_seconds
        self._max_entries = max_entries
        self._cache: dict[str, _Entry] = {}

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
            raise ExportError(
                ErrorCatalog.UNAVAILABLE, "renderer could not produce XLSX"
            ) from error
        if not content:
            raise ExportError(ErrorCatalog.UNAVAILABLE, "renderer produced empty content")

        artifact_id = self._new_id()
        now = self._clock()
        entry = _Entry(
            tenant_id=str(owner.tenant_id),
            user_id=str(owner.user_id),
            filename=filename,
            content_type=_XLSX_CONTENT_TYPE,
            content=content,
            expires_at=now + timedelta(seconds=self._retention_seconds),
        )
        await self._store.put(
            StoredArtifact(
                artifact_id=artifact_id,
                tenant_id=entry.tenant_id,
                user_id=entry.user_id,
                filename=entry.filename,
                content_type=entry.content_type,
                content=entry.content,
                expires_at=entry.expires_at,
            )
        )
        self._cache[artifact_id] = entry
        self._evict_expired(now)
        # Bound the read cache: evict the oldest entries beyond the cap.
        if len(self._cache) > self._max_entries:
            for oldest in sorted(self._cache, key=lambda key: self._cache[key].expires_at)[
                : len(self._cache) - self._max_entries
            ]:
                self._cache.pop(oldest, None)
        return ExportOutcome(
            artifact_id=artifact_id,
            filename=filename,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    async def fetch(self, owner: InteractionOwner, artifact_id: str) -> ExportContent | None:
        """Return the content when owned and still within the retention window.

        A missing, expired, or foreign id is indistinguishable (``None``):
        regeneration happens through history/favorite re-ask. A restart only
        clears the read cache — the store keeps serving within retention.
        """
        now = self._clock()
        self._evict_expired(now)
        cached = self._cache.get(artifact_id)
        if cached is not None:
            return self._content_for(owner, artifact_id, cached)
        stored = await self._store.get(artifact_id)
        if stored is None:
            return None
        if stored.expires_at <= now:
            # Lazy retention: nothing else visits an expired artifact, so the
            # first read after expiry is also what removes it.
            await self._store.delete(artifact_id)
            return None
        entry = _Entry(
            tenant_id=stored.tenant_id,
            user_id=stored.user_id,
            filename=stored.filename,
            content_type=stored.content_type,
            content=stored.content,
            expires_at=stored.expires_at,
        )
        self._cache[artifact_id] = entry
        return self._content_for(owner, artifact_id, entry)

    def _evict_expired(self, now: datetime) -> None:
        expired = [key for key, entry in self._cache.items() if entry.expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)

    @staticmethod
    def _content_for(
        owner: InteractionOwner, artifact_id: str, entry: _Entry
    ) -> ExportContent | None:
        if entry.tenant_id != str(owner.tenant_id) or entry.user_id != str(owner.user_id):
            return None
        return ExportContent(
            artifact_id=artifact_id,
            filename=entry.filename,
            content_type=entry.content_type,
            content=entry.content,
        )


def _timestamp_label(clock: Callable[[], datetime]) -> str:
    return clock().strftime("%Y%m%d%H%M%S")


__all__ = [
    "DEFAULT_EXPORT_MAX_ENTRIES",
    "DEFAULT_EXPORT_RETENTION_SECONDS",
    "ExportService",
]
