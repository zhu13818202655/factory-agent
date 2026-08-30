"""PostgreSQL connection management (Story 10).

The API process owns a read-only connection pool and never writes; the
generator process opens its own write connections and commits inside explicit
transactions. Credentials come only from the environment (``MOCK_MES_DATABASE_URL``).
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class MockMesDb:
    """Lifespan-managed read-only pool for the API process."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._pool: AsyncConnectionPool[Any] | None = None

    async def open(self) -> None:
        self._pool = AsyncConnectionPool(
            self._url,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await self._pool.open()
        # Fail fast: a missing data base must be a loud startup error, not a
        # silent in-memory fallback (Story 10 acceptance).
        async with self.pool.connection() as connection:
            await connection.execute("SELECT 1")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> AsyncConnectionPool[Any]:
        if self._pool is None:
            raise RuntimeError("mock-mes database pool is not open")
        return self._pool

    async def execute(self, sql: str, params: dict[str, object] | tuple[object, ...] = ()) -> Any:
        """Run a read-only statement and return the cursor (API side)."""
        async with self.pool.connection() as connection:
            return await connection.execute(sql, params)

    async def fetch(
        self, sql: str, params: dict[str, object] | tuple[object, ...] = ()
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def fetchone(
        self, sql: str, params: dict[str, object] | tuple[object, ...] = ()
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None
