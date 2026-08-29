"""Shared request-to-``PlatformScope`` resolution.

Token-first (Technology Notes): when ``Authorization: Bearer <token>`` is
present it wins; otherwise the trusted-gateway three headers remain the
development/test direct channel. Both channels produce the same
``PlatformScope`` semantics (3.7: token and header behavior must be
identical).
"""

from __future__ import annotations

from fastapi import Request

from usage_admin.auth import AuthService
from usage_admin.platform import (
    PRINCIPAL_HEADER,
    ROLE_HEADER,
    TENANT_HEADER,
    PlatformScope,
    PlatformScopeError,
    resolve_platform_scope,
)

_BEARER_PREFIX = "bearer "


def resolve_request_scope(request: Request, auth: AuthService) -> PlatformScope:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith(_BEARER_PREFIX):
        token = authorization[len(_BEARER_PREFIX) :].strip()
        scope = auth.resolve_token(token)
        if scope is None:
            raise PlatformScopeError("bearer token is invalid or expired")
        return scope
    return resolve_platform_scope(
        request.headers.get(PRINCIPAL_HEADER),
        request.headers.get(ROLE_HEADER),
        request.headers.get(TENANT_HEADER),
    )


__all__ = ["resolve_request_scope"]
