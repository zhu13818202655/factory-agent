"""Factory-account management endpoints (F2.1~F2.6).

Writes are admin-only (D14) and fully audited; DELETE is a soft disable (D10).
AppKey values are masked in every response except the single create response
that returns the plaintext key once (D9).
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from usage_admin.api.security import resolve_request_scope
from usage_admin.container import AdminContainer
from usage_admin.masking import mask_app_key
from usage_admin.platform import PlatformScope, PlatformScopeError
from usage_admin.store import TenantRegistryRecord
from usage_admin.tenants import (
    TenantRegistryError,
    TenantRegistryPage,
    TenantRegistryService,
)

tenants_router = APIRouter(prefix="/admin/v1/tenants", tags=["tenants"])


class RegistryItemOut(BaseModel):
    app_key: str
    tenant_name: str
    status: str
    created_at: datetime
    updated_at: datetime


class RegistryPageOut(BaseModel):
    items: list[RegistryItemOut]
    total: int
    next_cursor: int | None = None
    timezone: str


class RegistryCreateRequest(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=256)
    status: str = "active"
    app_key: str | None = Field(default=None, max_length=128)


class RegistryUpdateRequest(BaseModel):
    tenant_name: str | None = Field(default=None, min_length=1, max_length=256)
    status: str | None = None


def _tenants(request: Request) -> TenantRegistryService:
    return cast(AdminContainer, request.app.state.container).tenants


def _scope(request: Request) -> PlatformScope:
    try:
        container = cast(AdminContainer, request.app.state.container)
        return resolve_request_scope(request, container.auth)
    except PlatformScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@tenants_router.get("/registry", response_model=RegistryPageOut)
async def list_registry(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RegistryPageOut:
    scope = _scope(request)
    page_size = 20 if limit is None else limit
    try:
        page: TenantRegistryPage = await _tenants(request).list(
            scope, limit=page_size, offset=offset
        )
    except TenantRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RegistryPageOut(
        items=[_to_item(record, mask=True) for record in page.items],
        total=page.total,
        next_cursor=page.next_cursor,
        timezone="Asia/Shanghai",
    )


@tenants_router.get("/registry/{app_key}", response_model=RegistryItemOut)
async def get_registry(request: Request, app_key: str) -> RegistryItemOut:
    scope = _scope(request)
    record = await _tenants(request).get(scope, app_key)
    if record is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _to_item(record, mask=True)


@tenants_router.post("/registry", response_model=RegistryItemOut, status_code=201)
async def create_registry(request: Request, body: RegistryCreateRequest) -> RegistryItemOut:
    scope = _scope(request)
    try:
        record = await _tenants(request).create(
            scope,
            tenant_name=body.tenant_name,
            status=body.status,
            app_key=body.app_key,
        )
    except (PlatformScopeError, TenantRegistryError) as exc:
        status = 403 if isinstance(exc, PlatformScopeError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    # D9: the create response is the only response carrying the plaintext key.
    return _to_item(record, mask=False)


@tenants_router.patch("/registry/{app_key}", response_model=RegistryItemOut)
async def update_registry(
    request: Request, app_key: str, body: RegistryUpdateRequest
) -> RegistryItemOut:
    scope = _scope(request)
    if body.tenant_name is None and body.status is None:
        detail = "at least one of tenant_name/status is required"
        raise HTTPException(status_code=422, detail=detail)
    try:
        record = await _tenants(request).update(
            scope, app_key, tenant_name=body.tenant_name, status=body.status
        )
    except (PlatformScopeError, TenantRegistryError) as exc:
        status = 403 if isinstance(exc, PlatformScopeError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _to_item(record, mask=True)


@tenants_router.delete("/registry/{app_key}", status_code=204)
async def disable_registry(request: Request, app_key: str) -> None:
    scope = _scope(request)
    try:
        await _tenants(request).disable(scope, app_key)
    except PlatformScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except TenantRegistryError as exc:
        if str(exc) == "tenant not found":
            raise HTTPException(status_code=404, detail="tenant not found") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@tenants_router.post("/registry/{app_key}/enable", response_model=RegistryItemOut)
async def enable_registry(request: Request, app_key: str) -> RegistryItemOut:
    scope = _scope(request)
    try:
        record = await _tenants(request).enable(scope, app_key)
    except (PlatformScopeError, TenantRegistryError) as exc:
        status = 403 if isinstance(exc, PlatformScopeError) else 422
        if str(exc) == "tenant not found":
            raise HTTPException(status_code=404, detail="tenant not found") from exc
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _to_item(record, mask=True)


def _to_item(record: TenantRegistryRecord, *, mask: bool) -> RegistryItemOut:
    app_key = record.app_key if not mask else mask_app_key(record.app_key)
    return RegistryItemOut(
        app_key=app_key if app_key is not None else "",
        tenant_name=record.tenant_name,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


__all__ = ["tenants_router"]
