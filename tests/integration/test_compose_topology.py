from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_compose(name: str) -> dict[str, Any]:
    path = REPOSITORY_ROOT / "deploy" / "compose" / name
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def test_application_compose_contains_all_services() -> None:
    services = cast(dict[str, dict[str, Any]], load_compose("compose.yaml")["services"])

    # The statistics surface runs inside agent-api, so there is exactly one
    # application service; a second one reappearing here would mean the split
    # came back.
    assert set(services) == {
        "agent-api",
        "assistant-web",
        "postgres",
        "redis",
        "seaweedfs",
    }
    assert services["agent-api"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "redis": {"condition": "service_healthy"},
    }


def test_application_compose_mounts_both_export_fallbacks() -> None:
    """Both artifact backends keep a writable path under the read-only rootfs.

    ``agent-exports`` serves the conversation artifact exporter and
    ``agent-statistics-exports`` serves the platform report exporter. They are
    deliberately separate directories and separate buckets: sharing either one
    would make the two exporters overwrite each other's configuration.
    """
    services = cast(dict[str, dict[str, Any]], load_compose("compose.yaml")["services"])
    volumes = services["agent-api"]["volumes"]

    assert "agent-exports:/app/data/exports" in volumes
    assert "agent-statistics-exports:/app/data/statistics-exports" in volumes
    assert set(load_compose("compose.yaml")["volumes"]) >= {
        "agent-exports",
        "agent-statistics-exports",
        "seaweedfs-data",
    }


def test_middleware_compose_contains_only_local_dependencies() -> None:
    document = load_compose("middleware.yaml")
    services = cast(dict[str, dict[str, Any]], document["services"])

    assert set(services) == {"postgres", "redis", "seaweedfs"}
    assert services["postgres"]["image"] == "postgres:16-alpine"
    assert services["redis"]["image"] == "redis:7-alpine"
    assert services["seaweedfs"]["image"] == "chrislusf/seaweedfs:4.46"
    assert services["postgres"]["ports"] == ["127.0.0.1:${POSTGRES_PORT:-3432}:5432"]
    assert services["redis"]["ports"] == ["127.0.0.1:${REDIS_PORT:-3379}:6379"]
    assert services["seaweedfs"]["ports"] == ["127.0.0.1:${SEAWEEDFS_S3_PORT:-8333}:8333"]
