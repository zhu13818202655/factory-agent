"""The ``/v1/statistics`` router.

One router for the whole statistics surface, mounted on the main application by
``factory_agent.api.server``. Paths keep their original shape below the prefix:
the old ``/admin/v1`` segment became ``/v1/statistics`` and nothing else moved,
so a consumer only rewrites the base path.
"""

from fastapi import APIRouter

from factory_agent.statistics.api.auth import auth_router
from factory_agent.statistics.api.ops import ops_router
from factory_agent.statistics.api.tenants import tenants_router
from factory_agent.statistics.paths import STATISTICS_PREFIX

statistics_router = APIRouter(prefix=STATISTICS_PREFIX, tags=["statistics"])
statistics_router.include_router(auth_router)
statistics_router.include_router(tenants_router)
statistics_router.include_router(ops_router)


__all__ = ["STATISTICS_PREFIX", "statistics_router"]
