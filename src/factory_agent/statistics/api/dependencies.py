"""Shared statistics route dependencies.

Every handler resolves its identity through ``request_scope`` and its services
through ``statistics_container``. Both read the statistics container only:
neither a business credential nor a business container may be used to infer a
platform identity, and vice versa.
"""

from typing import cast

from fastapi import HTTPException, Request

from factory_agent.statistics.api.security import resolve_request_scope
from factory_agent.statistics.container import StatisticsContainer
from factory_agent.statistics.platform import PlatformScope, PlatformScopeError


def statistics_container(request: Request) -> StatisticsContainer:
    container = getattr(request.app.state, "statistics", None)
    if container is None:
        raise HTTPException(status_code=503, detail="statistics surface is not configured")
    return cast(StatisticsContainer, container)


def request_scope(request: Request) -> PlatformScope:
    try:
        return resolve_request_scope(request, statistics_container(request).auth)
    except PlatformScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


__all__ = ["request_scope", "statistics_container"]
