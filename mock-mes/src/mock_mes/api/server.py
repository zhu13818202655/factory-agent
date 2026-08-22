from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_mes import __version__
from mock_mes.api.canonical import CanonicalError
from mock_mes.api.canonical import router as canonical_router
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


async def canonical_error_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, CanonicalError):
        raise error
    return JSONResponse(
        status_code=error.status_code,
        content={
            "code": error.code,
            "message": error.message,
            "trace_id": "00000000000000000000000000000000",
        },
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
    app.add_exception_handler(CanonicalError, canonical_error_handler)
    app.include_router(health_router)
    app.include_router(canonical_router)
    return app
