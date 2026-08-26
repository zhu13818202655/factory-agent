"""Artifact store adapters (Story 6): filesystem fake and object-key safety."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from factory_agent.domain.errors import InvalidRequestError
from factory_agent.export.artifacts import (
    FilesystemArtifactStore,
    S3ArtifactStore,
)


def test_filesystem_store_roundtrip_put_get_delete() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = FilesystemArtifactStore(Path(directory))
        artifact_id = "abc123"

        async def exercise() -> None:
            await store.put(artifact_id, b"payload", "application/octet-stream")
            assert await store.get(artifact_id) == b"payload"
            await store.delete(artifact_id)
            with pytest.raises(Exception):
                await store.get(artifact_id)

        import asyncio

        asyncio.run(exercise())


def test_filesystem_store_rejects_path_traversal_keys() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = FilesystemArtifactStore(Path(directory))

        async def exercise() -> None:
            for bad in ("../x", "a/b", "a\\b", ".."):
                with pytest.raises(InvalidRequestError):
                    await store.put(bad, b"x", "text/plain")

        import asyncio

        asyncio.run(exercise())


def test_filesystem_store_never_writes_outside_base_dir() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        store = FilesystemArtifactStore(base)

        async def exercise() -> None:
            with pytest.raises(InvalidRequestError):
                await store.put("../../outside", b"x", "text/plain")

        import asyncio

        asyncio.run(exercise())
        assert list(base.iterdir()) == []


def test_s3_object_key_is_opaquely_prefixed_and_safe() -> None:
    store = S3ArtifactStore("http://seaweed:9000", "exports", path_prefix="factory-agent")
    key = store.object_key("deadbeef")
    assert key == "factory-agent/deadbeef"
    assert "/" not in store.object_key("a")[len("factory-agent/") :]


def test_s3_object_key_rejects_unsafe_identifiers() -> None:
    store = S3ArtifactStore("http://seaweed:9000", "exports")
    for bad in ("../x", "a/b", ".."):
        with pytest.raises(InvalidRequestError):
            store.object_key(bad)
