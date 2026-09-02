"""Artifact metadata repository port.

Separate from the object-storage ``ArtifactStore``: the repository records only
approved metadata (opaque object key, owner, capability, filename, size, SHA-256,
retention timestamps) and never stores employee IDs, names, question text, or
amounts. Every read is keyed by the trusted ownership pair so a cross-tenant
object key is unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner


class ErrorCatalog:
    """Bounded export error categories; never carries sensitive values."""

    NOT_FOUND = "artifact_not_found"
    UPLOAD_FAILED = "artifact_upload_failed"
    INVALID = "artifact_invalid"


class ExportError(Exception):
    """Structured exporter failure with a bounded category code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExportOutcome:
    """Result of a successful (or intentionally failed) export."""

    artifact_id: str
    filename: str
    size_bytes: int
    sha256: str
    expires_at: datetime
    download_url: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    tenant_id: TenantId
    user_id: UserId
    interaction_id: str
    capability_id: CapabilityId
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    expires_at: datetime


class ArtifactRepository(Protocol):
    async def save(self, record: ArtifactRecord) -> None: ...

    async def get(self, owner: InteractionOwner, artifact_id: str) -> ArtifactRecord | None: ...

    async def delete(self, artifact_id: str) -> None: ...

    async def list_expired(self, now: datetime) -> tuple[ArtifactRecord, ...]: ...


class ArtifactExporter(Protocol):
    """Renders a run result into an XLSX artifact and records its metadata."""

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

    async def presign(self, owner: InteractionOwner, artifact_id: str) -> str: ...

    async def cleanup(self, now: datetime) -> int: ...


__all__ = [
    "ArtifactExporter",
    "ArtifactRecord",
    "ArtifactRepository",
    "ErrorCatalog",
    "ExportError",
    "ExportOutcome",
]
