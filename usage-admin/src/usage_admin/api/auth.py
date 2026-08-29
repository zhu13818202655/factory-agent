"""Platform account registration and login (D15).

``/auth/register`` is admin-only; ``/auth/login`` is unauthenticated by design
(it *is* the authentication step) and returns a signed Bearer token. Front-end
integration never calls these endpoints — it uses ``USAGE_ADMIN_API_TOKEN``
(D16).
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from usage_admin.api.security import resolve_request_scope
from usage_admin.auth import AuthError, AuthService, PrincipalView
from usage_admin.container import AdminContainer
from usage_admin.platform import PlatformScopeError

auth_router = APIRouter(prefix="/admin/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    role: str = "viewer"
    tenant_scope: list[str] = Field(default_factory=list)


class RegisterResponse(BaseModel):
    principal_id: str
    username: str
    role: str
    tenant_scope: list[str]
    status: str
    created_at: datetime
    updated_at: datetime


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int


def _auth(request: Request) -> AuthService:
    return cast(AdminContainer, request.app.state.container).auth


@auth_router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(request: Request, body: RegisterRequest) -> RegisterResponse:
    try:
        scope = resolve_request_scope(request, _auth(request))
    except PlatformScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        view = await _auth(request).register(
            scope,
            username=body.username,
            password=body.password,
            role=body.role,
            tenant_scope=tuple(body.tenant_scope),
        )
    except (PlatformScopeError, AuthError) as exc:
        status = 403 if isinstance(exc, PlatformScopeError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _to_register(view)


@auth_router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest) -> LoginResponse:
    auth = _auth(request)
    try:
        token = await auth.login(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return LoginResponse(token=token, expires_in=auth.token_ttl_seconds)


def _to_register(view: PrincipalView) -> RegisterResponse:
    return RegisterResponse(
        principal_id=view.principal_id,
        username=view.username,
        role=view.role,
        tenant_scope=list(view.tenant_scope),
        status=view.status,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


__all__ = ["auth_router"]
