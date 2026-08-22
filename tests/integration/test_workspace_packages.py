from __future__ import annotations

from importlib.metadata import version


def test_workspace_installs_all_applications() -> None:
    assert version("factory-agent") == "0.1.0"
    assert version("mock-mes") == "0.1.0"
    assert version("usage-admin") == "0.1.0"
