"""Shared request-to-``PlatformScope`` resolution.

``Authorization: Bearer <token>`` is the only channel (D-2): a signed login
token from ``/v1/statistics/auth/login`` or the configured statistics API token.
Any other request is rejected — a header that merely asserts a principal and a
role is an unauthenticated identity back door, and this process already carries
the business surface's own credential header.
"""

from fastapi import Request

from factory_agent.statistics.platform import PlatformScope, PlatformScopeError
from factory_agent.statistics.platform_auth import AuthService

_BEARER_PREFIX = "bearer "


def resolve_request_scope(request: Request, auth: AuthService) -> PlatformScope:
    authorization = request.headers.get("Authorization", "")
    if not authorization.lower().startswith(_BEARER_PREFIX):
        raise PlatformScopeError("bearer token is required")
    token = authorization[len(_BEARER_PREFIX) :].strip()
    scope = auth.resolve_token(token)
    if scope is None:
        raise PlatformScopeError("bearer token is invalid or expired")
    return scope


__all__ = ["resolve_request_scope"]
