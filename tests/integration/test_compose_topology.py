from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_compose(name: str) -> dict[str, Any]:
    path = REPOSITORY_ROOT / "deploy" / "compose" / name
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def load_compose_env_template() -> dict[str, str]:
    """The active ``KEY=VALUE`` assignments of ``deploy/compose/.env.example``.

    Commented-out lines are skipped: the template documents keys it deliberately
    leaves unset, and those must not read back as configured.
    """
    path = REPOSITORY_ROOT / "deploy" / "compose" / ".env.example"
    assignments: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        assignments[key.strip()] = value.strip()
    return assignments


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


def test_application_compose_injects_the_operator_env_file() -> None:
    """Everything in ``deploy/compose/.env`` reaches ``agent-api``.

    Forwarding used to be enumerated by hand under ``environment:``, so adding a
    reviewed knob to the env file (``FACTORY_AGENT_MES_TIMEOUT_SECONDS`` and the
    other MES tuning values) silently had no effect inside the container.
    ``env_file`` makes that one file the single place an operator edits; dropping
    it re-opens the trap.
    """
    services = cast(dict[str, dict[str, Any]], load_compose("compose.yaml")["services"])

    assert {"path": ".env", "required": False} in services["agent-api"]["env_file"]


def test_application_compose_pins_container_only_settings() -> None:
    """These three must stay literals, because ``environment:`` beats ``env_file:``.

    The env file is also written for host-side runs, where the application is in
    development mode and binds loopback. Turning these into ``${...}``
    interpolations would let a host-oriented value follow the file into the
    container.
    """
    services = cast(dict[str, dict[str, Any]], load_compose("compose.yaml")["services"])
    environment = cast(dict[str, Any], services["agent-api"]["environment"])

    assert environment["FACTORY_AGENT_ENVIRONMENT"] == "production"
    assert environment["FACTORY_AGENT_HOST"] == "0.0.0.0"
    assert environment["FACTORY_AGENT_PORT"] == 8000


def test_compose_env_template_does_not_pin_host_loopback_databases() -> None:
    """The container must reach PostgreSQL and Redis by service name.

    ``compose.yaml`` resolves both through ``${VAR:-<service name>}``. That
    interpolation happens while the file is parsed, so it is reached before
    ``env_file`` precedence ever applies: assigning either key in the template
    replaces the service name with the host-side value, and the container then
    dials its own loopback instead of the sibling service. Host-based runs take
    their values from the repository-root ``.env``, which targets the ports
    published by ``middleware.yaml``.
    """
    assignments = load_compose_env_template()

    # Guard against a vacuously passing parse: the template must still expose the
    # keys an operator is meant to edit.
    assert "FACTORY_AGENT_MES_TIMEOUT_SECONDS" in assignments
    assert "FACTORY_AGENT_POSTGRES_URL" not in assignments
    assert "FACTORY_AGENT_REDIS_URL" not in assignments
