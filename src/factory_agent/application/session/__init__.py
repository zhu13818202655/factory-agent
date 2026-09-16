"""Bounded session orchestration.

This package owns the state machine, clarification budget, idempotent
execution, and durable event sequence, while identity resolution, capability
execution, and model calls stay behind ports. Authorization always completes
before any business-data call, and scope identifiers reach the executor only
through ``NarrowedFilters``.

Layout (each mixin layer only calls layers below it):

- ``definitions`` — limits, run state, terminal constants, shared logger
- ``base`` — ``SessionCore``: dependency wiring + shared primitives
- ``outcomes`` — phase / clarification / chat / termination events
- ``consistency`` — pre-execution scope guard + role-consistency safety net
- ``pipeline`` — the conversation pipeline from claim to durable terminal
- ``history`` — ownership resolution, loading, multi-turn context rebuild
- ``lifecycle`` — executor registry, follow loop, stale-run recovery, drain
- ``executor`` — background owner of one claimed interaction pipeline
- ``service`` — ``SessionService`` public API (start / stream / cancel)
"""

from factory_agent.application.session.definitions import (
    DrillPayload,
    InteractionNotFoundError,
    SessionLimits,
    StartRequest,
)
from factory_agent.application.session.executor import InteractionRunExecutor
from factory_agent.application.session.service import SessionService

__all__ = [
    "DrillPayload",
    "InteractionNotFoundError",
    "InteractionRunExecutor",
    "SessionLimits",
    "SessionService",
    "StartRequest",
]
