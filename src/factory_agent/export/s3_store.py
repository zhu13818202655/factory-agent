"""S3-compatible object store backend (SeaweedFS in the reference deployment).

Objects are laid out as ``<prefix><artifact_id>.xlsx`` plus a sibling
``<prefix><artifact_id>.json`` carrying the owner binding and display fields.
The sidecar (rather than S3 user metadata) keeps display filenames out of HTTP
header fields, where non-ASCII values would need ad-hoc encoding.

Read failures degrade to an ordinary miss (``None``), matching the local
backend: a missing, unreadable, or foreign export id must stay
indistinguishable to the caller. Write failures raise ``ExportError`` with
``UNAVAILABLE`` so a download id is never handed out for bytes that never
landed.

``aioboto3`` and ``botocore`` ship no type stubs, so their client surface is
pinned to ``Any`` at exactly two boundaries — the session attribute and the
module imports — and every value read back out is annotated before use.
Nothing loosely typed escapes the store's public contract.
"""

from typing import Any, cast

import aioboto3  # type: ignore[reportMissingTypeStubs]
from botocore.config import Config  # type: ignore[reportMissingTypeStubs]
from botocore.exceptions import (  # type: ignore[reportMissingTypeStubs]
    BotoCoreError,
    ClientError,
)

from factory_agent.export.metadata import ArtifactMetadata, decode_metadata, encode_metadata
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.artifacts import ErrorCatalog, ExportError, StoredArtifact

_LOGGER = get_logger("factory_agent.export.s3_store")

DEFAULT_OBJECT_PREFIX = "exports/"
_JSON_CONTENT_TYPE = "application/json"
#: Bounded client retries: an unreachable object store must fail the export
#: quickly instead of stalling the interaction's compose stage.
_MAX_ATTEMPTS = 2
_CONNECT_TIMEOUT_SECONDS = 3
_READ_TIMEOUT_SECONDS = 10
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class S3ArtifactStore:
    """Retained exports in an S3-compatible bucket, keyed by artifact id."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        path_style: bool = True,
        prefix: str = DEFAULT_OBJECT_PREFIX,
        session: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix if prefix.endswith("/") else f"{prefix}/"
        #: Injectable so the store's error mapping can be exercised without a
        #: live gateway; production always builds its own session.
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

    async def put(self, artifact: StoredArtifact) -> None:
        """Store bytes first, then the sidecar, so a readable id always has bytes."""
        try:
            async with self._session.client("s3", **self._client_kwargs) as client:
                await client.put_object(
                    Bucket=self._bucket,
                    Key=self._object_key(artifact.artifact_id),
                    Body=artifact.content,
                    ContentType=artifact.content_type,
                )
                await client.put_object(
                    Bucket=self._bucket,
                    Key=self._metadata_key(artifact.artifact_id),
                    Body=encode_metadata(ArtifactMetadata.of(artifact)),
                    ContentType=_JSON_CONTENT_TYPE,
                )
        except (ClientError, BotoCoreError, OSError) as error:
            _LOGGER.warning(
                "export.s3.put_failed",
                artifact_id=artifact.artifact_id,
                error=_error_code(error),
            )
            raise ExportError(ErrorCatalog.UNAVAILABLE, "artifact store is not writable") from error

    async def get(self, artifact_id: str) -> StoredArtifact | None:
        try:
            async with self._session.client("s3", **self._client_kwargs) as client:
                response: Any = await client.get_object(
                    Bucket=self._bucket, Key=self._metadata_key(artifact_id)
                )
                blob = await _read_body(response)
                body: Any = await client.get_object(
                    Bucket=self._bucket, Key=self._object_key(artifact_id)
                )
                content = await _read_body(body)
        except ClientError as error:
            if _error_code(error) not in _MISSING_CODES:
                _LOGGER.warning(
                    "export.s3.read_failed", artifact_id=artifact_id, error=_error_code(error)
                )
            return None
        except (BotoCoreError, OSError, KeyError, TypeError) as error:
            _LOGGER.warning(
                "export.s3.read_failed", artifact_id=artifact_id, error=_error_code(error)
            )
            return None
        metadata = decode_metadata(blob)
        if metadata is None:
            _LOGGER.warning("export.store.corrupt_meta", artifact_id=artifact_id)
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

    async def delete(self, artifact_id: str) -> None:
        """Best-effort removal of both objects."""
        try:
            async with self._session.client("s3", **self._client_kwargs) as client:
                for key in (self._metadata_key(artifact_id), self._object_key(artifact_id)):
                    await client.delete_object(Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError, OSError) as error:
            _LOGGER.warning(
                "export.store.purge_failed", artifact_id=artifact_id, error=_error_code(error)
            )

    def _object_key(self, artifact_id: str) -> str:
        return f"{self._prefix}{artifact_id}.xlsx"

    def _metadata_key(self, artifact_id: str) -> str:
        return f"{self._prefix}{artifact_id}.json"


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


__all__ = ["DEFAULT_OBJECT_PREFIX", "S3ArtifactStore"]
