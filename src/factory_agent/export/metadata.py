"""Persisted artifact metadata, shared by every storage backend.

Both backends store the owner binding next to the bytes, in the same JSON
shape, so switching backends never changes who can download what. The codec
stays free of retention policy: ``ExportService`` decides expiry, the backend
only round-trips what it was given.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from factory_agent.ports.artifacts import StoredArtifact


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Owner binding plus display fields for one retained artifact."""

    tenant_id: str
    user_id: str
    filename: str
    content_type: str
    expires_at: datetime

    @classmethod
    def of(cls, artifact: StoredArtifact) -> "ArtifactMetadata":
        return cls(
            tenant_id=artifact.tenant_id,
            user_id=artifact.user_id,
            filename=artifact.filename,
            content_type=artifact.content_type,
            expires_at=artifact.expires_at,
        )


def encode_metadata(metadata: ArtifactMetadata) -> bytes:
    return json.dumps(
        {
            "tenant_id": metadata.tenant_id,
            "user_id": metadata.user_id,
            "filename": metadata.filename,
            "content_type": metadata.content_type,
            "expires_at": metadata.expires_at.isoformat(),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def decode_metadata(blob: bytes) -> ArtifactMetadata | None:
    """``None`` for anything unreadable: corrupt, truncated, or missing fields."""
    try:
        raw = json.loads(blob.decode("utf-8"))
        return ArtifactMetadata(
            tenant_id=str(raw["tenant_id"]),
            user_id=str(raw["user_id"]),
            filename=str(raw["filename"]),
            content_type=str(raw["content_type"]),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["ArtifactMetadata", "decode_metadata", "encode_metadata"]
