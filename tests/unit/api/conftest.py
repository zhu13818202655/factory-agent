from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_export_store(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep generated artifacts out of the repository working tree.

    ``tests/conftest.py`` deletes every ``FACTORY_AGENT_*`` variable so the real
    ``.env`` cannot leak into the suite, which also means a test that builds the
    application from real settings never finds an S3 endpoint and falls back to
    the default local artifact store at ``data/exports``. The pipeline exports on
    every completed interaction, and nothing ever fetches or purges a test
    artifact, so without this the suite leaves retained exports in the working
    tree that outlive their own retention window.
    """
    monkeypatch.setenv("FACTORY_AGENT_EXPORT_STORE_DIR", str(tmp_path / "exports"))
    yield
