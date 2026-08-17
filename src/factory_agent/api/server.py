from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from factory_agent import __version__


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["factory-agent"]
    version: str


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok", service="factory-agent", version=__version__)


@health_router.get("/ready", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    return HealthResponse(status="ok", service="factory-agent", version=__version__)


def create_app() -> FastAPI:
    app = FastAPI(title="factory-agent", version=__version__)
    app.include_router(health_router)
    return app
