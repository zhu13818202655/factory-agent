"""Database engine construction for the session store and the migration runner.

The project ships psycopg 3, but a bare ``postgresql://`` DSN makes SQLAlchemy
reach for psycopg2. Both the async store and the sync Alembic runner therefore
go through :func:`normalize_dsn`, which pins the driver that is actually
installed. Callers keep passing the plain DSN from configuration.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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


def create_session_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        normalize_dsn(url),
        pool_size=pool_size,
        pool_pre_ping=True,
    )


def create_migration_engine(url: str) -> sa.Engine:
    return sa.create_engine(normalize_dsn(url), poolclass=sa.pool.NullPool)


__all__ = ["DRIVER", "create_migration_engine", "create_session_engine", "normalize_dsn"]
