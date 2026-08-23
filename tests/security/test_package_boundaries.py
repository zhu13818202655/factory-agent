import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROOT = REPOSITORY_ROOT / "src" / "factory_agent"
USAGE_ADMIN_ROOT = REPOSITORY_ROOT / "usage-admin" / "src" / "usage_admin"

ALLOWED_PRODUCT_DEPENDENCIES: dict[str, set[str]] = {
    "api": {"application", "bootstrap", "config", "domain", "observability"},
    "application": {"domain", "ports"},
    "domain": set(),
    "ports": {"domain"},
    "data_api": {"domain", "ports"},
    "execution": {"domain", "ports"},
    "persistence": {"config", "domain", "ports"},
    "llm": {"domain", "ports"},
    "export": {"domain", "ports"},
    "observability": {"config", "domain", "ports"},
}


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


def imported_product_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    packages: set[str] = set()
    for node in ast.walk(tree):
        module_names: list[str] = []
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_names.append(node.module)

        for module_name in module_names:
            parts = module_name.split(".")
            if len(parts) >= 2 and parts[0] == "factory_agent":
                packages.add(parts[1])
    return packages


def test_product_does_not_import_external_application_packages() -> None:
    forbidden_roots = {"mock_mes", "report_agent", "vanna"}
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if imported_roots(path) & forbidden_roots
    ]

    assert offenders == []


def test_mock_mes_does_not_import_product() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "mock-mes" / "src" / "mock_mes")
        if "factory_agent" in imported_roots(path)
    ]

    assert offenders == []


def test_usage_admin_does_not_import_product_or_mock_mes() -> None:
    forbidden_roots = {"factory_agent", "mock_mes"}
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(USAGE_ADMIN_ROOT)
        if imported_roots(path) & forbidden_roots
    ]

    assert offenders == []


def test_product_and_mock_mes_do_not_import_usage_admin() -> None:
    roots = (PRODUCT_ROOT, REPOSITORY_ROOT / "mock-mes" / "src" / "mock_mes")
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for root in roots
        for path in python_files(root)
        if "usage_admin" in imported_roots(path)
    ]

    assert offenders == []


def test_only_data_api_may_import_httpx_in_product_code() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if "httpx" in imported_roots(path) and "data_api" not in path.parts
    ]

    assert offenders == []


def test_product_package_dependencies_follow_architecture() -> None:
    offenders: list[str] = []
    for package, allowed_dependencies in ALLOWED_PRODUCT_DEPENDENCIES.items():
        for path in python_files(PRODUCT_ROOT / package):
            forbidden = imported_product_packages(path) - allowed_dependencies - {package}
            if forbidden:
                relative_path = path.relative_to(REPOSITORY_ROOT)
                offenders.append(f"{relative_path}: {', '.join(sorted(forbidden))}")

    assert offenders == []
