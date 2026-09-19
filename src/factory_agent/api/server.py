import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from factory_agent.statistics.api.router import statistics_router

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

    A third loop maintains the ``usage_event`` monthly partitions. Metering is
    isolated (a failed write is alerted, never rolled back into the answer), so
    a missing partition would otherwise be silent data loss; keeping the current
    and the following month present means crossing a month boundary is a
    non-event.

    A fourth loop recomputes the hourly/daily rollups that every reported KPI
    reads. Unlike a missed partition this is not data loss — the facts are all
    still there — but it is equally invisible: an unrun rollup reads as zero
    traffic on every dashboard, which is indistinguishable from a quiet day.
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

    # Out-of-band LLM endpoint health: one startup probe so the first request
    # already routes against the current verdict, then a periodic probe so a
    # server that dies while the process lives is demoted within the cadence.
    # Both are bounded (timeout per endpoint) and any failure is logged and
    # dropped; the reviewed registry stays in place, litellm's own fallback
    # still covers the request path.
    health = container.model_health
    health_task: asyncio.Task[None] | None = None
    if health is not None:
        try:
            await health.refresh()
        except Exception:  # noqa: BLE001 - probing must never block startup
            _logger.exception("llm.health.startup_probe_failed")
        if settings.llm_health_probe_interval_seconds > 0:
            health_task = asyncio.create_task(
                health.probe_forever(settings.llm_health_probe_interval_seconds),
                name="llm-endpoint-health",
            )
    partitions = container.usage_partitions
    partition_task: asyncio.Task[None] | None = None
    if partitions is not None:
        try:
            await partitions.ensure()
        except Exception:  # noqa: BLE001 - maintenance must never block startup
            _logger.exception("usage.partition.startup_failed")
        if settings.usage_partition_sweep_interval_seconds > 0:
            partition_task = asyncio.create_task(
                partitions.ensure_forever(settings.usage_partition_sweep_interval_seconds),
                name="usage-event-partitions",
            )
    # Every reported KPI reads the rollup tables, so the first pass runs before
    # the app serves traffic: an instance brought up after downtime answers with
    # real numbers instead of zeros. A failure here is alerted and dropped —
    # reporting must never block startup.
    rollup = container.usage_rollup
    rollup_task: asyncio.Task[None] | None = None
    if rollup is not None:
        try:
            await rollup.run_once(datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 - reporting must never block startup
            _logger.exception("usage.rollup.startup_failed")
        if settings.usage_rollup_sweep_interval_seconds > 0:
            rollup_task = asyncio.create_task(
                rollup.run_forever(),
                name="usage-rollup",
            )
    try:
        yield
    finally:
        if rollup_task is not None:
            rollup_task.cancel()
            await asyncio.gather(rollup_task, return_exceptions=True)
        if partition_task is not None:
            partition_task.cancel()
            await asyncio.gather(partition_task, return_exceptions=True)
        if health_task is not None:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
        if health is not None:
            try:
                await health.aclose()
            except Exception:  # noqa: BLE001 - shutdown must never hang the app
                _logger.exception("llm.health.aclose_failed")
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
    container = build_container(resolved_settings, overrides)
    app.state.container = container
    app.state.settings = resolved_settings
    # The statistics surface resolves its own container, so a platform request
    # can never be served by reading the business container's state.
    app.state.statistics = container.statistics

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
    app.include_router(statistics_router)
    return app
