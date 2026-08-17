from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def test_product_does_not_import_mock_mes() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if "mock_mes" in imported_roots(path)
    ]

    assert offenders == []


def test_mock_mes_does_not_import_product() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "mock-mes" / "src" / "mock_mes")
        if "factory_agent" in imported_roots(path)
    ]

    assert offenders == []


def test_only_data_api_may_import_httpx_in_product_code() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if "httpx" in imported_roots(path) and "data_api" not in path.parts
    ]

    assert offenders == []
