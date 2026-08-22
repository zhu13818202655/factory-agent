from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel

from usage_admin import __version__
from usage_admin.config import UsageAdminSettings, get_settings


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: Literal["usage-admin"]
    version: str
    database: Literal["configured", "not_configured"] | None = None


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse, response_model_exclude_none=True)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok", service="usage-admin", version=__version__)


@health_router.get("/ready", response_model=HealthResponse)
async def readiness(request: Request) -> HealthResponse:
    settings = cast(UsageAdminSettings, request.app.state.settings)
    if settings.database_url is None:
        return HealthResponse(
            status="degraded",
            service="usage-admin",
            version=__version__,
            database="not_configured",
        )
    return HealthResponse(
        status="ok",
        service="usage-admin",
        version=__version__,
        database="configured",
    )


def create_app(settings: UsageAdminSettings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    app = FastAPI(title="usage-admin", version=__version__)
    app.state.settings = active_settings
    app.include_router(health_router)
    return app
