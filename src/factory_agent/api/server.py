from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, cast

from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel

from factory_agent import __version__
from factory_agent.api.exports import export_router
from factory_agent.api.personal import personal_router
from factory_agent.api.preferences import preferences_router
from factory_agent.api.sessions import session_router
from factory_agent.bootstrap import ApplicationContainer, DependencyOverrides, build_container
from factory_agent.config import FactoryAgentSettings, get_settings
from factory_agent.observability.context import (
    accept_request_id,
    bind_request_id,
)
from factory_agent.observability.logging_adapter import configure_logging


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
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    app = FastAPI(title="factory-agent", version=__version__)
    app.state.container = build_container(resolved_settings, overrides)
    app.state.settings = resolved_settings

    header_name = resolved_settings.request_id_header

    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(header_name)
        request_id = accept_request_id(inbound)
        bind_request_id(request_id)
        response = await call_next(request)
        response.headers[header_name] = request_id
        return response

    app.middleware("http")(request_id_middleware)

    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(export_router)
    app.include_router(personal_router)
    app.include_router(preferences_router)
    return app
