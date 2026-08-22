from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel

from factory_agent import __version__
from factory_agent.bootstrap import ApplicationContainer, DependencyOverrides, build_container
from factory_agent.config import FactoryAgentSettings, get_settings


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: Literal["factory-agent"]
    version: str
    dependencies: dict[str, str] | None = None


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse, response_model_exclude_none=True)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok", service="factory-agent", version=__version__)


@health_router.get("/ready", response_model=HealthResponse)
async def readiness(request: Request) -> HealthResponse:
    container = cast(ApplicationContainer, request.app.state.container)
    is_ready = all(value != "not_configured" for value in container.readiness.values())
    return HealthResponse(
        status="ok" if is_ready else "degraded",
        service="factory-agent",
        version=__version__,
        dependencies=container.readiness,
    )


def create_app(
    settings: FactoryAgentSettings | None = None,
    overrides: DependencyOverrides | None = None,
) -> FastAPI:
    app = FastAPI(title="factory-agent", version=__version__)
    app.state.container = build_container(settings or get_settings(), overrides)
    app.include_router(health_router)
    return app
