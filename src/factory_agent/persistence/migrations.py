

import argparse
from pathlib import Path
from typing import Sequence

from alembic import command
from alembic.config import Config

from factory_agent.config import get_settings
from factory_agent.persistence.engine import normalize_dsn


def _project_root() -> Path:
    """Directory holding alembic.ini + migrations/ (repo root, /app in image).

    Source-tree installs resolve via the package path; wheel installs
    (--no-editable) land in site-packages where parents[N] arithmetic would
    point into the venv, so walk upward for the alembic.ini marker instead.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "alembic.ini").is_file() and (candidate / "migrations").is_dir():
            return candidate
    return Path(__file__).resolve().parents[3]


def build_alembic_config() -> Config:
    settings = get_settings()
    if settings.postgres_url is None:
        raise SystemExit("FACTORY_AGENT_POSTGRES_URL is required for migrations")

    repository_root = _project_root()
    config = Config(repository_root / "alembic.ini")
    config.set_main_option("script_location", str(repository_root / "migrations"))
    config.set_main_option("sqlalchemy.url", normalize_dsn(str(settings.postgres_url)))
    return config


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage the factory-agent database schema")
    parser.add_argument("action", choices=("upgrade", "downgrade"))
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args(argv)
    config = build_alembic_config()
    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    else:
        command.downgrade(config, args.revision)


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    # Without this guard `python -m factory_agent.persistence.migrations upgrade
    # head` only imports the module and silently performs no migration, while
    # the `factory-agent-migrate` console script works. Both paths must behave
    # identically (the VS Code launch configs use the `-m` form).
    main()
