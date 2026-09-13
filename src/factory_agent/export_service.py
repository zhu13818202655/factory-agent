"""Instant export with local-disk retention (即时生成、直接下载、落盘保留).

Renders a ``CapabilityRunResult`` into XLSX fully in memory, then writes the
bytes plus a metadata sidecar into a local directory — a local stand-in for a
net-disk / object store (不引入 MinIO，先落本地文件). Downloads therefore
survive process restarts within the retention window, and expired artifacts
are purged lazily. Every fetch re-checks the persisted owner binding
(tenant/user); a missing, expired, or foreign export id stays an
indistinguishable 404 — regeneration goes through history/favorite re-ask.

The in-memory buffer is only a read cache in front of the disk; its entry cap
bounds memory, never availability. This module lives at the package root
because it composes the ``export`` renderer and the artifact store; the
session/application layers depend only on the ``ArtifactExporter`` port.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
)
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_LOGGER = get_logger("factory_agent.export_service")

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: Default retention before a generated export is purged from disk (seconds).
DEFAULT_EXPORT_RETENTION_SECONDS = 7 * 86400
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
        store_dir: Path,
        clock: Callable[[], datetime] | None = None,
        new_id: Callable[[], str] | None = None,
        retention_seconds: float = DEFAULT_EXPORT_RETENTION_SECONDS,
        max_entries: int = DEFAULT_EXPORT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._new_id = new_id or (lambda: uuid4().hex)
        self._store_dir = Path(store_dir)
        self._retention_seconds = retention_seconds
        self._max_entries = max_entries
        self._cache: dict[str, _Entry] = {}
        self._store_dir.mkdir(parents=True, exist_ok=True)

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
        await asyncio.to_thread(self._write_artifact, artifact_id, entry)
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
        clears the read cache — the disk store keeps serving within retention.
        """
        now = self._clock()
        self._evict_expired(now)
        cached = self._cache.get(artifact_id)
        if cached is not None:
            return self._content_for(owner, artifact_id, cached)
        entry = await asyncio.to_thread(self._load_artifact, artifact_id, now)
        if entry is None:
            return None
        self._cache[artifact_id] = entry
        return self._content_for(owner, artifact_id, entry)

    # ------------------------------------------------------------ disk store

    def _write_artifact(self, artifact_id: str, entry: _Entry) -> None:
        """Atomically persist bytes + sidecar metadata; failure fails the export.

        If the bytes could not be stored, the later download could never be
        served, so the export is rejected now instead of handing out a dead id.
        """
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            bytes_path = self._store_dir / f"{artifact_id}.xlsx"
            meta_path = self._store_dir / f"{artifact_id}.json"
            bytes_tmp = bytes_path.with_suffix(".xlsx.tmp")
            bytes_tmp.write_bytes(entry.content)
            bytes_tmp.replace(bytes_path)
            meta_tmp = meta_path.with_suffix(".json.tmp")
            meta_tmp.write_text(
                json.dumps(
                    {
                        "tenant_id": entry.tenant_id,
                        "user_id": entry.user_id,
                        "filename": entry.filename,
                        "content_type": entry.content_type,
                        "expires_at": entry.expires_at.isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            meta_tmp.replace(meta_path)
        except OSError as error:
            raise ExportError(ErrorCatalog.UNAVAILABLE, "artifact store is not writable") from error

    def _load_artifact(self, artifact_id: str, now: datetime) -> _Entry | None:
        meta_path = self._store_dir / f"{artifact_id}.json"
        bytes_path = self._store_dir / f"{artifact_id}.xlsx"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            expires_at = datetime.fromisoformat(str(meta["expires_at"]))
            entry = _Entry(
                tenant_id=str(meta["tenant_id"]),
                user_id=str(meta["user_id"]),
                filename=str(meta["filename"]),
                content_type=str(meta["content_type"]),
                content=b"",
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("export.store.corrupt_meta", artifact_id=artifact_id)
            return None
        if expires_at <= now:
            self._purge(artifact_id)
            return None
        try:
            content = bytes_path.read_bytes()
        except OSError:
            _LOGGER.warning("export.store.bytes_missing", artifact_id=artifact_id)
            return None
        return _Entry(
            tenant_id=entry.tenant_id,
            user_id=entry.user_id,
            filename=entry.filename,
            content_type=entry.content_type,
            content=content,
            expires_at=entry.expires_at,
        )

    def _evict_expired(self, now: datetime) -> None:
        expired = [key for key, entry in self._cache.items() if entry.expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)

    def _purge(self, artifact_id: str) -> None:
        """Best-effort removal of one expired artifact's files."""
        for suffix in (".json", ".xlsx"):
            try:
                (self._store_dir / f"{artifact_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                _LOGGER.warning("export.store.purge_failed", artifact_id=artifact_id)

    # ------------------------------------------------------------- ownership

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
