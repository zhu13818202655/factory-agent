"""S3-compatible artifact storage adapters (Story 6).

The application depends only on the ``ArtifactStore`` port. Two implementations
ship here:

- ``S3ArtifactStore`` talks to any S3-compatible object store (SeaweedFS is the
  local/private reference; the production selection is a Story 9 approval).
  Object keys are opaque UUIDs; the store never derives keys from employee IDs,
  question text, or amounts.
- ``FilesystemArtifactStore`` is the offline test fake; it stores content under a
  base directory and is never used in production.

Object stores and buckets are never made public, and no permanent object URL is
returned: download goes through a short-lived presigned link only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aioboto3  # pyright: ignore[reportMissingTypeStubs]

from factory_agent.domain.errors import InvalidRequestError, UpstreamUnavailableError
from factory_agent.ports.contracts import ArtifactStore


class FilesystemArtifactStore(ArtifactStore):
    """Offline filesystem-backed fake; not for production."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: str) -> Path:
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
            raise InvalidRequestError("artifact id is not a safe object key")
        return self._base_dir / artifact_id

    async def put(self, artifact_id: str, content: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._path(artifact_id).write_bytes, content)

    async def get(self, artifact_id: str) -> bytes:
        path = self._path(artifact_id)
        if not path.exists():
            raise UpstreamUnavailableError("artifact content not found")
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, artifact_id: str) -> None:
        path = self._path(artifact_id)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def presign(self, artifact_id: str, expires_in_seconds: int) -> str:
        # The filesystem fake cannot produce a real URL; it returns a signed
        # marker to the object key, still requiring the download path to
        # re-validate the tenant/credential.
        return f"fs://{self._path(artifact_id).name}?expires={expires_in_seconds}"


class S3ArtifactStore(ArtifactStore):
    """S3-compatible artifact store via aioboto3 (SeaweedFS/MinIO compatible)."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        *,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        path_prefix: str = "factory-agent",
        max_attempts: int = 3,
    ) -> None:
        self._endpoint = endpoint
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._prefix = path_prefix.rstrip("/")
        self._max_attempts = max_attempts

    def object_key(self, artifact_id: str) -> str:
        """Return the opaque object key; rejects unsafe identifiers."""
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
            raise InvalidRequestError("artifact id is not a safe object key")
        return f"{self._prefix}/{artifact_id}"

    def _session(self) -> Any:
        return aioboto3.Session()

    def _client(self) -> Any:
        session = self._session()
        return session.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    async def _call_with_retry(self, operation: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                async with self._client() as client:
                    return await operation(client, **kwargs)
            except Exception as error:  # noqa: BLE001 - retryable transport failures
                last_error = error
                if attempt + 1 < self._max_attempts:
                    await asyncio.sleep(0.2 * (attempt + 1))
        raise UpstreamUnavailableError("artifact store operation failed") from last_error

    async def put(self, artifact_id: str, content: bytes, content_type: str) -> None:
        key = self.object_key(artifact_id)

        async def _put(client: Any) -> None:
            await client.put_object(
                Bucket=self._bucket, Key=key, Body=content, ContentType=content_type
            )

        await self._call_with_retry(_put)

    async def get(self, artifact_id: str) -> bytes:
        key = self.object_key(artifact_id)

        async def _get(client: Any) -> bytes:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            return await body.read()

        return await self._call_with_retry(_get)

    async def delete(self, artifact_id: str) -> None:
        key = self.object_key(artifact_id)

        async def _delete(client: Any) -> None:
            await client.delete_object(Bucket=self._bucket, Key=key)

        await self._call_with_retry(_delete)

    async def presign(self, artifact_id: str, expires_in_seconds: int) -> str:
        key = self.object_key(artifact_id)

        async def _presign(client: Any) -> str:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in_seconds,
            )
            return str(url)

        return await self._call_with_retry(_presign)


__all__ = ["FilesystemArtifactStore", "S3ArtifactStore"]
