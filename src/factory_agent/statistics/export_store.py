"""Export artifact backends for platform report exports.

An export is rendered in memory and handed to an ``ExportFileStore`` under the
key ``exports/<export_id>.<format>``. Three backends implement that contract:
the local directory store (``FACTORY_AGENT_STATISTICS_EXPORT_STORE_DIR``), the S3-compatible
object store (``FACTORY_AGENT_STATISTICS_S3_ENDPOINT_URL``; SeaweedFS in the reference
deployment) and the in-memory store that dev runs and tests fall back to.

The backend only stores, reads and deletes bytes by key. Retention, ownership
and the signed short-lived download token all stay in ``ExportService`` and the
API layer, which keeps streaming the bytes itself — switching backends never
changes who may download what.

Read failures degrade to a miss (``None``) so a missing object stays
indistinguishable from an expired or foreign one. Write failures raise
``ExportStoreError``, which fails the export before a download link is handed
out for bytes that never landed.

``aioboto3`` and ``botocore`` ship no type stubs, so their surface is pinned to
``Any`` at the session attribute and the module imports only, and every value
read back out is annotated before use.
"""

import asyncio
from pathlib import Path
from typing import Any, Protocol, cast

import aioboto3  # type: ignore[reportMissingTypeStubs]
from botocore.config import Config  # type: ignore[reportMissingTypeStubs]
from botocore.exceptions import (  # type: ignore[reportMissingTypeStubs]
    BotoCoreError,
    ClientError,
)

from factory_agent.observability.logging_adapter import get_logger

_LOGGER = get_logger("factory_agent.statistics.export_store")

#: Bounded client retries: an unreachable object store must fail the export
#: quickly instead of stalling the export request.
_MAX_ATTEMPTS = 2
_CONNECT_TIMEOUT_SECONDS = 3
_READ_TIMEOUT_SECONDS = 10
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_OCTET_STREAM = "application/octet-stream"


class ErrorCode:
    """Bounded backend error categories; never carries sensitive values."""

    UNAVAILABLE = "export_store_unavailable"
    INVALID_KEY = "export_store_invalid_key"


class ExportStoreError(RuntimeError):
    """Structured backend failure with a bounded category code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def content_type_for(key: str) -> str:
    """Map an export key's extension to its media type."""
    return _CONTENT_TYPES.get(Path(key).suffix.lower(), _OCTET_STREAM)


def _safe_relative_key(key: str) -> Path:
    """Reject keys that could escape the store directory.

    Keys are built from a server-generated export id, so this is defence in
    depth: a traversal segment must fail loudly instead of writing outside the
    configured directory.
    """
    candidate = Path(key)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ExportStoreError(ErrorCode.INVALID_KEY, "export key escapes the store directory")
    return candidate


class LocalExportFileStore:
    """Export artifacts on the local filesystem, one directory per deployment."""

    def __init__(self, store_dir: Path) -> None:
        # The directory is created lazily on the first write: an unwritable
        # store must fail the export, not the service's startup.
        self._store_dir = Path(store_dir)

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._write, key, data)

    async def get(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._read, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    def _write(self, key: str, data: bytes) -> None:
        """Write atomically (temp file + rename) so no download can see a partial file."""
        path = self._store_dir / _safe_relative_key(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f"{path.suffix}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as error:
            _LOGGER.warning("export.store.write_failed", key=key, error=type(error).__name__)
            raise ExportStoreError(ErrorCode.UNAVAILABLE, "export store is not writable") from error

    def _read(self, key: str) -> bytes | None:
        path = self._store_dir / _safe_relative_key(key)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _delete(self, key: str) -> None:
        """Best-effort removal; a failure is logged, not raised."""
        path = self._store_dir / _safe_relative_key(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _LOGGER.warning("export.store.purge_failed", key=key)


class S3ExportFileStore:
    """Export artifacts in an S3-compatible bucket, keyed by export key."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        path_style: bool = True,
        session: Any | None = None,
    ) -> None:
        if not endpoint_url.strip():
            raise ValueError("endpoint_url is required for the S3 export store")
        if not bucket.strip():
            raise ValueError("bucket is required for the S3 export store")
        self._bucket = bucket
        #: Injectable so error mapping can be exercised without a live gateway;
        #: production always builds its own session.
        self._session: Any = session if session is not None else aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": endpoint_url,
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": Config(
                # Path style avoids a DNS lookup per bucket, which is what a
                # single-node gateway such as SeaweedFS expects.
                s3={"addressing_style": "path" if path_style else "auto"},
                retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                read_timeout=_READ_TIMEOUT_SECONDS,
            ),
        }

    async def put(self, key: str, data: bytes) -> None:
        object_key = _safe_relative_key(key).as_posix()
        try:
            async with self._session.client("s3", **self._client_kwargs) as client:
                await client.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=data,
                    ContentType=content_type_for(key),
                )
        except (ClientError, BotoCoreError, OSError) as error:
            _LOGGER.warning("export.store.write_failed", key=key, error=_error_code(error))
            raise ExportStoreError(ErrorCode.UNAVAILABLE, "export store is not writable") from error

    async def get(self, key: str) -> bytes | None:
        object_key = _safe_relative_key(key).as_posix()
        try:
            async with self._session.client("s3", **self._client_kwargs) as client:
                response: Any = await client.get_object(Bucket=self._bucket, Key=object_key)
                return await _read_body(response)
        except ClientError as error:
            if _error_code(error) not in _MISSING_CODES:
                _LOGGER.warning("export.store.read_failed", key=key, error=_error_code(error))
            return None
        except (BotoCoreError, OSError, KeyError, TypeError) as error:
            _LOGGER.warning("export.store.read_failed", key=key, error=_error_code(error))
            return None

    async def delete(self, key: str) -> None:
        """Best-effort removal of one object."""
        object_key = _safe_relative_key(key).as_posix()
        try:
            async with self._session.client("s3", **self._client_kwargs) as client:
                await client.delete_object(Bucket=self._bucket, Key=object_key)
        except (ClientError, BotoCoreError, OSError) as error:
            _LOGGER.warning("export.store.purge_failed", key=key, error=_error_code(error))


class InMemoryExportFileStore:
    """Non-persistent store: dev runs and tests only, lost on restart."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self._blobs[key] = data

    async def get(self, key: str) -> bytes | None:
        return self._blobs.get(key)

    async def delete(self, key: str) -> None:
        self._blobs.pop(key, None)

    def blob_keys(self) -> tuple[str, ...]:
        return tuple(self._blobs)


class ExportFileStore(Protocol):
    """Minimal storage contract shared by every export backend."""

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> None: ...


async def _read_body(response: Any) -> bytes:
    """Drain an S3 streaming body: loosely typed in, ``bytes`` out."""
    body: Any = response["Body"]
    content: Any = await body.read()
    return bytes(content)


def _error_code(error: BaseException) -> str:
    """Bounded error label for logs; never carries a URL, key, or credential."""
    if isinstance(error, ClientError):
        return _client_error_code(error)
    return type(error).__name__[:64]


def _client_error_code(error: ClientError) -> str:
    """Read ``Error.Code`` from an untyped botocore payload, defensively.

    Both levels are declared through ``cast`` so the payload stays opaque:
    narrowing ``Any`` with ``isinstance`` would degrade it to a mapping of
    ``Unknown`` and leak untyped values back into checkable code.
    """
    response = cast("dict[str, Any] | None", getattr(error, "response", None))
    block = (
        cast("dict[str, Any] | None", response.get("Error")) if isinstance(response, dict) else None
    )
    code = block.get("Code") if isinstance(block, dict) else None
    return code[:64] if isinstance(code, str) and code else "client_error"


__all__ = [
    "ErrorCode",
    "ExportFileStore",
    "ExportStoreError",
    "InMemoryExportFileStore",
    "LocalExportFileStore",
    "S3ExportFileStore",
    "content_type_for",
]
