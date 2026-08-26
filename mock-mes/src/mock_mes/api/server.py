"""Mock MES FastAPI application: customer-shaped endpoints (Story 5)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_mes import __version__
from mock_mes.api.customer import MesError
from mock_mes.api.customer import router as customer_router
from mock_mes.api.faults import FaultControlMiddleware
from mock_mes.config import MockMesSettings, get_settings
from mock_mes.seed import build_dataset


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["mock-mes"]
    version: str


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok", service="mock-mes", version=__version__)


@health_router.get("/ready", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    return HealthResponse(status="ok", service="mock-mes", version=__version__)


async def mes_error_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, MesError):
        raise error
    return JSONResponse(
        status_code=error.status_code,
        content={"code": 0, "message": error.message, "result": None, "timestamp": 0},
    )


def create_app(settings: MockMesSettings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    app = FastAPI(title="mock-mes", version=__version__)
    app.state.dataset = build_dataset(
        active_settings.scenario,
        active_settings.seed,
        active_settings.virtual_now,
    )
    app.add_middleware(FaultControlMiddleware)
    app.add_exception_handler(MesError, mes_error_handler)
    app.include_router(health_router)
    app.include_router(customer_router)
    return app
