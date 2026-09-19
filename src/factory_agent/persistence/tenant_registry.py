"""Read-only access to the ``tenant_registry`` table.

The table is owned by the statistics surface, which creates and writes it; this
service only reads the AppKey / status pair that drives the D13 pre-call guard
and the API edge's suspended-tenant gate. The table is deliberately not declared
in this service's ``tables.METADATA``, so the disposable test schema cannot
accidentally create or drop the statistics surface's own tables.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.ports.tenant_registry import TenantRegistryRecord

_TABLE = "tenant_registry"


class SqlTenantRegistryReader:
    """Reads tenant status for the MES pre-call guard."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, app_key: str) -> TenantRegistryRecord | None:
        statement = sa.text(
            f"SELECT app_key, tenant_name, status FROM {_TABLE} WHERE app_key = :app_key"
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement, {"app_key": app_key})).mappings().first()
        if row is None:
            return None
        return TenantRegistryRecord(
            app_key=str(row["app_key"]),
            tenant_name=str(row["tenant_name"]),
            status=str(row["status"]),
        )


__all__ = ["SqlTenantRegistryReader"]
