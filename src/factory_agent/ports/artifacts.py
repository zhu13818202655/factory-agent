"""Export artifact contract: render metadata, content, and the storage backend.

An export is rendered into XLSX in memory, handed to an ``ExportStore``
backend, and served back to its owner through the download endpoint, which
re-validates ownership and writes an audit event before releasing any bytes.

Two backends implement the same contract: the local directory store (default,
used by unit tests and host-only runs) and the S3-compatible object store
(production shape — see ``FACTORY_AGENT_S3_ENDPOINT_URL``). The backend is
deliberately dumb: it stores, reads, and deletes by id. Retention, ownership,
and the in-memory read cache all live in ``ExportService`` so both backends
behave identically. A missing, expired, or foreign export id stays an
indistinguishable 404; regeneration goes through history/favorite re-ask.
"""

from dataclasses import dataclass
from datetime import datetime
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
    """Export bytes served to the owner, carrying the download filename."""

    artifact_id: str
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """One retained export as the backend sees it: bytes plus owner binding.

    The owner binding (``tenant_id``/``user_id``) is persisted next to the
    bytes so a process restart never widens who can download it.
    """

    artifact_id: str
    tenant_id: str
    user_id: str
    filename: str
    content_type: str
    content: bytes
    expires_at: datetime


class ExportStore(Protocol):
    """Storage backend for retained exports, keyed by artifact id.

    Named distinctly from the legacy transient-content buffer in
    ``ports.contracts`` so a reader never has to guess which store a type
    annotation means. Implementations never decide retention or access: they
    persist what they are given and return what they find. A backend write
    failure raises ``ExportError`` with ``UNAVAILABLE`` — the export is then
    rejected instead of handing out an id that could never be downloaded.
    """

    async def put(self, artifact: StoredArtifact) -> None: ...

    async def get(self, artifact_id: str) -> StoredArtifact | None:
        """Return the stored export, or ``None`` when it is absent/unreadable."""
        ...

    async def delete(self, artifact_id: str) -> None:
        """Best-effort removal; a failure is logged by the implementation."""
        ...


class ArtifactExporter(Protocol):
    """Renders a run result into XLSX and hands back a retained export id."""

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
        """Return the content when owned and still within its window.

        A missing, expired, or foreign id is indistinguishable (``None``).
        """
        ...


__all__ = [
    "ArtifactExporter",
    "ErrorCatalog",
    "ExportContent",
    "ExportError",
    "ExportOutcome",
    "ExportStore",
    "StoredArtifact",
]
