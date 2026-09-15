"""Local-directory artifact store: the default backend and the test backend.

Writes ``<id>.xlsx`` plus a ``<id>.json`` sidecar (owner binding, display
filename, content type, expiry) into one directory. Writes are atomic
(temp file + rename) so a crash never leaves a half-written artifact that a
download could serve. This is the offline path: unit tests and host-only runs
use it, and it needs no object store.
"""

import asyncio
from pathlib import Path

from factory_agent.export.metadata import ArtifactMetadata, decode_metadata, encode_metadata
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.artifacts import ErrorCatalog, ExportError, StoredArtifact

_LOGGER = get_logger("factory_agent.export.local_store")

_BYTES_SUFFIX = ".xlsx"
_META_SUFFIX = ".json"


class LocalArtifactStore:
    """Retained exports on the local filesystem, one directory per deployment."""

    def __init__(self, store_dir: Path) -> None:
        # The directory is created lazily on the first write, not here: an
        # unwritable store (e.g. a read-only container FS without the export
        # volume) must degrade to "no export this time" instead of crashing
        # the whole service at startup.
        self._store_dir = Path(store_dir)

    async def put(self, artifact: StoredArtifact) -> None:
        await asyncio.to_thread(self._write, artifact)

    async def get(self, artifact_id: str) -> StoredArtifact | None:
        return await asyncio.to_thread(self._read, artifact_id)

    async def delete(self, artifact_id: str) -> None:
        await asyncio.to_thread(self._delete, artifact_id)

    def _write(self, artifact: StoredArtifact) -> None:
        """Atomically persist bytes + sidecar metadata; failure fails the export.

        If the bytes could not be stored, the later download could never be
        served, so the export is rejected now instead of handing out a dead id.
        """
        bytes_path = self._store_dir / f"{artifact.artifact_id}{_BYTES_SUFFIX}"
        meta_path = self._store_dir / f"{artifact.artifact_id}{_META_SUFFIX}"
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            bytes_tmp = bytes_path.with_suffix(".xlsx.tmp")
            bytes_tmp.write_bytes(artifact.content)
            bytes_tmp.replace(bytes_path)
            meta_tmp = meta_path.with_suffix(".json.tmp")
            meta_tmp.write_bytes(encode_metadata(ArtifactMetadata.of(artifact)))
            meta_tmp.replace(meta_path)
        except OSError as error:
            raise ExportError(ErrorCatalog.UNAVAILABLE, "artifact store is not writable") from error

    def _read(self, artifact_id: str) -> StoredArtifact | None:
        meta_path = self._store_dir / f"{artifact_id}{_META_SUFFIX}"
        bytes_path = self._store_dir / f"{artifact_id}{_BYTES_SUFFIX}"
        try:
            blob = meta_path.read_bytes()
        except OSError:
            return None
        metadata = decode_metadata(blob)
        if metadata is None:
            _LOGGER.warning("export.store.corrupt_meta", artifact_id=artifact_id)
            return None
        try:
            content = bytes_path.read_bytes()
        except OSError:
            _LOGGER.warning("export.store.bytes_missing", artifact_id=artifact_id)
            return None
        return StoredArtifact(
            artifact_id=artifact_id,
            tenant_id=metadata.tenant_id,
            user_id=metadata.user_id,
            filename=metadata.filename,
            content_type=metadata.content_type,
            content=content,
            expires_at=metadata.expires_at,
        )

    def _delete(self, artifact_id: str) -> None:
        """Best-effort removal of one artifact's files."""
        for suffix in (_META_SUFFIX, _BYTES_SUFFIX):
            try:
                (self._store_dir / f"{artifact_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                _LOGGER.warning("export.store.purge_failed", artifact_id=artifact_id)


__all__ = ["LocalArtifactStore"]
