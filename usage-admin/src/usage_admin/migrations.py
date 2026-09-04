from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from alembic import command
from alembic.config import Config

from usage_admin.config import get_settings

DRIVER = "psycopg"


def normalize_dsn(url: str) -> str:
    """Pin the installed driver unless the DSN already names one."""
    scheme, separator, remainder = url.partition("://")
    if not separator:
        raise ValueError("database URL must include a scheme")
    if "+" in scheme:
        return url
    if scheme not in {"postgresql", "postgres"}:
        raise ValueError("only PostgreSQL URLs are supported")
    return f"postgresql+{DRIVER}://{remainder}"


def build_alembic_config() -> Config:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("USAGE_ADMIN_DATABASE_URL is required for migrations")

    project_root = Path(__file__).resolve().parents[2]
    config = Config(project_root / "alembic.ini")
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", normalize_dsn(settings.database_url.get_secret_value())
    )
    return config


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage the usage-admin database schema")
    parser.add_argument("action", choices=("upgrade", "downgrade"))
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args(argv)
    config = build_alembic_config()
    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    else:
        command.downgrade(config, args.revision)


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    # Without this guard `python -m usage_admin.migrations upgrade head` only
    # imports the module and silently performs no migration, while the
    # `usage-admin-migrate` console script works. Both paths must behave
    # identically (the VS Code launch configs use the `-m` form).
    main()
