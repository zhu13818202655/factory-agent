"""Trusted identity resolution at the API edge.

Identity comes only from the customer token exchange. The caller presents the
encrypted ``app_key`` in the configured credential header and the token gateway
turns it into a ``ResolvedPrincipal`` (authoritative role + bound departments).
The body never carries tenant, user, or scope fields.

A successful exchange is also the one moment this service learns that a factory
exists, so it is where a first-seen AppKey is registered (R1). Registration
never blocks the call: a failed ledger write is alerted and the question
proceeds.

Resolution is memoised on the request, because both the router-level guard and
the handler need it: without the memo the exchange (a live MES call) and the
first-seen upsert would run twice per request.

``require_tenant_enabled`` is the router-level guard that turns a suspended
factory away before any work happens (R3). It is a dependency rather than
per-handler code so a newly added route cannot forget it.

When no token gateway is configured (offline unit tests / degraded mode), the
service falls back to the trusted-gateway tenant and user headers so it stays
exercisable. Production deployments always configure the gateway (a live MES
base URL is present in the settings), so in production identity is sourced
exclusively from the token (customer contract §2). The fallback is deliberately
narrow and never active alongside a configured gateway.
"""

from typing import cast

from fastapi import HTTPException, Request, status

from factory_agent.api.tenant_lifecycle import TENANT_DISABLED_DETAIL
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

type ResolvedCredential = tuple[TrustedCredential, ResolvedPrincipal | None]

_CREDENTIAL_ATTRIBUTE = "factory_agent_resolved_credential"


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


async def resolve_credential(request: Request) -> ResolvedCredential:
    """Resolve the caller's trusted credential, preferring the token exchange.

    Returns the credential plus the resolved principal (``None`` only in the
    degraded header-fallback path). Raises HTTP 401 for missing/invalid
    credentials and 502 when the token endpoint cannot complete the exchange.
    The result is remembered for the rest of the request, so the guard and the
    handler share one exchange; a rejection is never remembered.
    """
    memo: object | None = getattr(request.state, _CREDENTIAL_ATTRIBUTE, None)
    if memo is not None:
        return cast(ResolvedCredential, memo)
    resolved = await _resolve_credential(request)
    setattr(request.state, _CREDENTIAL_ATTRIBUTE, resolved)
    return resolved


async def _resolve_credential(request: Request) -> ResolvedCredential:
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
        lifecycle = container.tenant_lifecycle
        if lifecycle is not None:
            await lifecycle.register_seen(str(principal.credential.tenant_id))
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


async def require_tenant_enabled(request: Request) -> None:
    """Reject a suspended factory before the handler runs any work (R3).

    The guard sits on the business routers, so a suspended tenant gets a 403
    instead of a parsed intent, a model call, and a stored conversation. The
    statistics surface is deliberately not guarded: platform operators must
    still be able to read a suspended factory's history and audit trail.
    """
    lifecycle = _container(request).tenant_lifecycle
    if lifecycle is None or lifecycle.reader is None:
        return
    credential, _ = await resolve_credential(request)
    if await lifecycle.is_disabled(str(credential.tenant_id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=TENANT_DISABLED_DETAIL)


__all__ = [
    "TENANT_HEADER",
    "USER_HEADER",
    "require_tenant_enabled",
    "resolve_credential",
]
