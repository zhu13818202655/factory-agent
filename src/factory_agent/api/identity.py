"""Trusted identity resolution at the API edge.

Identity comes only from the customer token exchange. The caller presents the
encrypted ``app_key`` in the configured credential header and the token gateway
turns it into a ``ResolvedPrincipal`` (authoritative role + bound departments).
The body never carries tenant, user, or scope fields.

When no token gateway is configured (offline unit tests / degraded mode), the
service falls back to the trusted-gateway tenant and user headers so it stays
exercisable. Production deployments always configure the gateway (a live MES
base URL is present in the settings), so in production identity is sourced
exclusively from the token (customer contract §2). The fallback is deliberately
narrow and never active alongside a configured gateway.
"""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException, Request, status

from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import TenantId, UserId
from factory_agent.domain.errors import (
    InvalidRequestError,
    MesError,
    UnauthenticatedError,
)
from factory_agent.ports import TrustedCredential
from factory_agent.ports.contracts import ResolvedPrincipal

#: Degraded-mode trusted-gateway headers; unused when the token gateway is live.
TENANT_HEADER = "X-Factory-Tenant-Id"
USER_HEADER = "X-Factory-User-Id"


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


async def resolve_credential(
    request: Request,
) -> tuple[TrustedCredential, ResolvedPrincipal | None]:
    """Resolve the caller's trusted credential, preferring the token exchange.

    Returns the credential plus the resolved principal (``None`` only in the
    degraded header-fallback path). Raises HTTP 401 for missing/invalid
    credentials and 502 when the token endpoint cannot complete the exchange.
    """
    container = _container(request)
    exchange = container.credential_exchange
    if exchange is not None:
        raw = request.headers.get(container.settings.credential_header)
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="credential header is missing",
            )
        try:
            principal = await exchange.authenticate(raw)
        except UnauthenticatedError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="credential was rejected"
            ) from exc
        except InvalidRequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="credential is invalid"
            ) from exc
        except MesError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="token exchange is unavailable",
            ) from exc
        return principal.credential, principal

    tenant_id = request.headers.get(TENANT_HEADER)
    user_id = request.headers.get(USER_HEADER)
    if not tenant_id or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted identity headers are missing"
        )
    try:
        credential = TrustedCredential(tenant_id=TenantId(tenant_id), user_id=UserId(user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted identity headers are invalid"
        ) from exc
    return credential, None


__all__ = ["TENANT_HEADER", "USER_HEADER", "resolve_credential"]
