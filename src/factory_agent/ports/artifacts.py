"""Instant-export contract (Story 3: 即时生成、直接下载、服务端不留存).

The exporter renders a run result into XLSX in memory and keeps it only in a
bounded transient buffer for a short window; there is no object store, no
presigned URL, no retention lifecycle, and no durable artifact record. The
download endpoint streams the transient bytes back as a file response after
re-validating ownership. "回头再取" is served by history/favorite re-ask
(重新执行 → 直接下载), never by a stored file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from factory_agent.domain import CapabilityId
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner


class ErrorCatalog:
    """Bounded export error categories; never carries sensitive values."""

    NOT_FOUND = "artifact_not_found"
    UNAVAILABLE = "artifact_unavailable"


class ExportError(Exception):
    """Structured exporter failure with a bounded category code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExportOutcome:
    """Result of an instant export: an id plus render metadata."""

    artifact_id: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExportContent:
    """Transient export bytes served to the owner within the short window."""

    artifact_id: str
    filename: str
    content_type: str
    content: bytes


class ArtifactExporter(Protocol):
    """Renders a run result into XLSX and hands back a transient export id."""

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
    ) -> ExportOutcome: ...

    async def fetch(self, owner: InteractionOwner, artifact_id: str) -> ExportContent | None:
        """Return the transient content when owned and still within its window.

        A missing, expired, or foreign id is indistinguishable (``None``): the
        client should regenerate via history/favorite re-ask.
        """
        ...


__all__ = [
    "ArtifactExporter",
    "ErrorCatalog",
    "ExportContent",
    "ExportError",
    "ExportOutcome",
]
