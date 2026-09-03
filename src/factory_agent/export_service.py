"""Instant, no-retention export service (Story 3).

Renders a ``CapabilityRunResult`` into XLSX fully in memory and keeps the bytes
only in a bounded, short-TTL in-process buffer (受控临时缓冲). There is no
object store write, no presigned URL, and no retention lifecycle — the bytes
are released when the download response finishes or the window expires, and
"回头再取" is served by history/favorite re-ask (重新执行 → 直接下载).

This module lives at the package root because it composes the ``export``
renderer and the transient buffer; the session/application layers depend only
on the ``ArtifactExporter`` port.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from factory_agent.domain import CapabilityId
from factory_agent.execution.kernel import render_table_from_run_result
from factory_agent.export.sanitize import build_export_filename
from factory_agent.export.xlsx import render_xlsx
from factory_agent.ports.artifacts import (
    ErrorCatalog,
    ExportContent,
    ExportError,
    ExportOutcome,
)
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: Default transient window before a generated export is dropped (seconds).
DEFAULT_EXPORT_TTL_SECONDS = 900
#: Hard cap on buffered exports so a busy tenant cannot exhaust memory.
DEFAULT_EXPORT_MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class _Entry:
    tenant_id: str
    user_id: str
    filename: str
    content_type: str
    content: bytes
    created_monotonic: float


class ExportService:
    """Renders a ``CapabilityRunResult`` into transient, downloadable XLSX."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        new_id: Callable[[], str] | None = None,
        ttl_seconds: float = DEFAULT_EXPORT_TTL_SECONDS,
        max_entries: int = DEFAULT_EXPORT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._new_id = new_id or (lambda: uuid4().hex)
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._buffer: dict[str, _Entry] = {}

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
        self._evict_expired()
        self._buffer[artifact_id] = _Entry(
            tenant_id=str(owner.tenant_id),
            user_id=str(owner.user_id),
            filename=filename,
            content_type=_XLSX_CONTENT_TYPE,
            content=content,
            created_monotonic=time.monotonic(),
        )
        # Bound the buffer: evict the oldest entries beyond the cap.
        if len(self._buffer) > self._max_entries:
            for oldest in sorted(self._buffer, key=lambda key: self._buffer[key].created_monotonic)[
                : len(self._buffer) - self._max_entries
            ]:
                self._buffer.pop(oldest, None)
        return ExportOutcome(
            artifact_id=artifact_id,
            filename=filename,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    async def fetch(self, owner: InteractionOwner, artifact_id: str) -> ExportContent | None:
        """Return the transient content when owned and still within its window.

        A missing, expired, or foreign id is indistinguishable (``None``):
        regeneration happens through history/favorite re-ask, never a stored
        file.
        """
        self._evict_expired()
        entry = self._buffer.get(artifact_id)
        if entry is None:
            return None
        if entry.tenant_id != str(owner.tenant_id) or entry.user_id != str(owner.user_id):
            return None
        return ExportContent(
            artifact_id=artifact_id,
            filename=entry.filename,
            content_type=entry.content_type,
            content=entry.content,
        )

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl_seconds
        expired = [
            artifact_id
            for artifact_id, entry in self._buffer.items()
            if entry.created_monotonic < cutoff
        ]
        for artifact_id in expired:
            self._buffer.pop(artifact_id, None)


def _timestamp_label(clock: Callable[[], datetime]) -> str:
    return clock().strftime("%Y%m%d%H%M%S")


__all__ = [
    "DEFAULT_EXPORT_MAX_ENTRIES",
    "DEFAULT_EXPORT_TTL_SECONDS",
    "ExportService",
]
