from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_compose(name: str) -> dict[str, Any]:
    path = REPOSITORY_ROOT / "deploy" / "compose" / name
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def test_application_compose_contains_all_services() -> None:
    services = cast(dict[str, dict[str, Any]], load_compose("compose.yaml")["services"])

    assert set(services) == {"agent-api", "mock-mes", "postgres", "redis", "usage-admin"}
    assert services["agent-api"]["depends_on"] == {
        "mock-mes": {"condition": "service_healthy"},
        "postgres": {"condition": "service_healthy"},
        "redis": {"condition": "service_healthy"},
    }
    assert services["mock-mes"]["build"]["dockerfile"] == "mock-mes/Dockerfile"
    assert services["usage-admin"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert services["usage-admin"]["build"]["dockerfile"] == "usage-admin/Dockerfile"


def test_middleware_compose_contains_only_local_dependencies() -> None:
    document = load_compose("middleware.yaml")
    services = cast(dict[str, dict[str, Any]], document["services"])

    assert set(services) == {"postgres", "redis"}
    assert services["postgres"]["image"] == "postgres:16-alpine"
    assert services["redis"]["image"] == "redis:7-alpine"
    assert services["postgres"]["ports"] == ["127.0.0.1:${POSTGRES_PORT:-5432}:5432"]
    assert services["redis"]["ports"] == ["127.0.0.1:${REDIS_PORT:-6379}:6379"]
