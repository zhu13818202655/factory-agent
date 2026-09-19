"""App assembly for statistics tests.

The statistics surface is a router on the main application, so tests mount it
the same way ``factory_agent.api.server`` does: one FastAPI instance, the
router, and the container on ``app.state.statistics``.
"""

from fastapi import FastAPI

from factory_agent.statistics.api.router import statistics_router
from factory_agent.statistics.container import StatisticsContainer


def build_statistics_app(container: StatisticsContainer) -> FastAPI:
    app = FastAPI(title="statistics-test")
    app.state.statistics = container
    app.include_router(statistics_router)
    return app


__all__ = ["build_statistics_app"]
