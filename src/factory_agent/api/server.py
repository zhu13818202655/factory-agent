import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
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
from factory_agent.observability.logging_adapter import configure_logging, get_logger

_logger = get_logger("factory_agent.api.server")


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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup recovery, periodic sweep, and shutdown drain for the executors.

    Startup: durably fail every ``running`` interaction left orphaned by a
    previous process and every ``pending`` interaction that no stream ever
    claimed (bulk compare-and-sets, idempotent under multi-worker starts). The
    periodic sweep then keeps reaping both classes while the process lives, so
    an orphan whose followers all disconnected converges without a restart.
    Shutdown: stop the sweep loop, then give the in-process executors a bounded
    drain window; whatever is still running is cancelled and recovered by the
    next startup sweep.
    """
    container = cast(ApplicationContainer, app.state.container)
    settings = cast(FactoryAgentSettings, app.state.settings)
    service = container.sessions_service
    sweep_task: asyncio.Task[None] | None = None
    if service is not None:
        try:
            await service.sweep_abandoned_runs()
            await service.sweep_stale_runs()
        except Exception:  # noqa: BLE001 - recovery must never block startup
            _logger.exception("session.sweep.startup_failed")
        if settings.session_sweep_interval_seconds > 0:
            sweep_task = asyncio.create_task(
                service.sweep_forever(settings.session_sweep_interval_seconds),
                name="session-recovery-sweep",
            )
    try:
        yield
    finally:
        if sweep_task is not None:
            sweep_task.cancel()
            await asyncio.gather(sweep_task, return_exceptions=True)
        if service is not None:
            try:
                await service.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must never hang the app
                _logger.exception("session.shutdown.drain_failed")


def create_app(
    settings: FactoryAgentSettings | None = None,
    overrides: DependencyOverrides | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    app = FastAPI(title="factory-agent", version=__version__, lifespan=_lifespan)
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
