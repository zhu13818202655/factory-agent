"""Mock MES FastAPI application: customer-shaped endpoints (Story 5, PG-backed).

Since Story 10 the app owns a read-only PostgreSQL pool (``MockMesDb``) and a
``MockMesStore`` facade; there is no in-memory dataset and no fallback. A
missing ``MOCK_MES_DATABASE_URL`` is a loud startup error, and readiness checks
the live database connection.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_mes import __version__
from mock_mes.api.customer import MesError
from mock_mes.api.customer import router as customer_router
from mock_mes.api.faults import FaultControlMiddleware
from mock_mes.config import MockMesSettings, get_settings
from mock_mes.db import MockMesDb
from mock_mes.store import MockMesStore


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["mock-mes"]
    version: str


health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok", service="mock-mes", version=__version__)


@health_router.get("/ready", response_model=HealthResponse)
async def readiness(request: Request) -> HealthResponse:
    # Readiness means the data base is reachable: PG is the only data source.
    await request.app.state.db.execute("SELECT 1")
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
    database_url = (
        active_settings.database_url.get_secret_value() if active_settings.database_url else None
    )
    if database_url is None:
        # Story 10: PG is the only data source; there is no in-memory fallback.
        raise RuntimeError("MOCK_MES_DATABASE_URL is required (in-memory dataset removed)")

    db = MockMesDb(database_url)
    store = MockMesStore(db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await db.open()
        yield
        await db.close()

    app = FastAPI(title="mock-mes", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.store = store
    app.add_middleware(FaultControlMiddleware)
    app.add_exception_handler(MesError, mes_error_handler)
    app.include_router(health_router)
    app.include_router(customer_router)
    return app
