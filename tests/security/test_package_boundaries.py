import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROOT = REPOSITORY_ROOT / "src" / "factory_agent"

ALLOWED_PRODUCT_DEPENDENCIES: dict[str, set[str]] = {
    "api": {"application", "bootstrap", "config", "domain", "observability", "ports", "statistics"},
    "application": {"domain", "observability", "ports"},
    "domain": set(),
    "ports": {"domain"},
    "data_api": {"domain", "observability", "ports"},
    # kernel.py logs structured events via observability.logging_adapter, the
    # same cross-cutting logger used by application/data_api/persistence.
    "execution": {"domain", "observability", "ports"},
    "persistence": {"config", "domain", "observability", "ports"},
    "llm": {"domain", "ports"},
    # local_store.py/s3_store.py log store anomalies (corrupt metadata, purge
    # failures) through observability.logging_adapter, the same cross-cutting
    # logger used by application/data_api/execution/persistence.
    "export": {"domain", "observability", "ports"},
    "observability": {"config", "domain", "ports"},
    "infrastructure": {"domain", "ports"},
    # The statistics surface is a peer context, not a business one: it may read
    # the shared database and log, and nothing else. Deliberately absent are
    # application/domain/data_api/ports, so the platform surface can never grow
    # a business dependency and stays extractable if that is ever needed.
    "statistics": {"config", "observability"},
}

#: Packages the statistics surface must never reach into. It reports on MES
#: traffic; it never makes MES calls of its own.
STATISTICS_FORBIDDEN_PACKAGES: set[str] = {"data_api"}

#: Packages that must never import the statistics surface. The business
#: runtime knows nothing about platform reporting.
STATISTICS_UNREACHABLE_FROM: set[str] = {"application", "domain"}

#: Packages allowed to own an outbound HTTP client. ``data_api`` is the only one
#: permitted to reach MES endpoints. ``llm`` is deliberately absent: outbound
#: model traffic goes through the litellm SDK, not a hand-rolled HTTP client.
HTTP_BOUNDARY_PACKAGES: set[str] = {"data_api"}

#: Only the model gateway adapter may import the litellm SDK (ADR-0006).
LLM_SDK_PACKAGES: set[str] = {"llm"}


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
    forbidden_roots = {"report_agent", "vanna"}
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if imported_roots(path) & forbidden_roots
    ]

    assert offenders == []


def test_only_http_boundary_packages_may_import_httpx() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if "httpx" in imported_roots(path) and not HTTP_BOUNDARY_PACKAGES & set(path.parts)
    ]

    assert offenders == []


def test_only_data_api_may_reach_mes_endpoints() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(REPOSITORY_ROOT / "src" / "factory_agent")
        if "data_api" not in path.parts and "canonical_mes" in path.read_text(encoding="utf-8")
    ]

    assert offenders == ["src/factory_agent/bootstrap.py", "src/factory_agent/config.py"]


def test_only_the_model_gateway_may_import_the_litellm_sdk() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(PRODUCT_ROOT)
        if "litellm" in imported_roots(path) and not LLM_SDK_PACKAGES & set(path.parts)
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


def test_statistics_never_imports_an_excluded_package() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in python_files(PRODUCT_ROOT / "statistics")
        if imported_product_packages(path) & STATISTICS_FORBIDDEN_PACKAGES
    ]

    assert offenders == []


def test_business_runtime_never_imports_statistics() -> None:
    offenders: list[str] = []
    for package in STATISTICS_UNREACHABLE_FROM:
        for path in python_files(PRODUCT_ROOT / package):
            if "statistics" in imported_product_packages(path):
                offenders.append(str(path.relative_to(REPOSITORY_ROOT)))

    assert offenders == []
