"""Token exchange gateway: the only component calling ``/api/system/token``.

Exchanges the caller's encrypted ``app_key`` for the customer credential
bundle and exposes only identity facts (``ResolvedPrincipal``) to the API and
application layers. Credential values (encrypted app_key, accessToken, sign,
app_key) never leave this module: they are never logged, traced, echoed in
errors, or returned through the exchange ports.

Runtime binding:
- One live bundle per authenticated caller, keyed by ``(tenant_id, user_id)``.
- ``bind_for`` exposes the caller's bundle through a context variable for the
  duration of one interaction; the MES adapter reads it at send time. Outside
  any binding the adapter falls back to its injected default bundle.
- Refresh re-exchanges the stored encrypted credential exactly once per
  failure ("请求已过期"/"签名无效") and proactively when the accessToken
  approaches expiry or the short-lived ``timestamp`` window (60 s) closes.

Contract sources: ``docs/product/AI问答对外接口-整理.md`` §2 and
``docs/product/需求及方案整理.md``「客户确认结论」.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Generator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx

from factory_agent.data_api.credentials import CURRENT_BUNDLE, MesCredentialBundle
from factory_agent.data_api.hongzhao import map_message_to_error
from factory_agent.data_api.schemas import CredentialBundleResponse, MesEnvelope
from factory_agent.domain import DeptId, EmployeeId, Role, TenantId, TenantMembership, UserId
from factory_agent.domain.errors import (
    MesError,
    UnauthenticatedError,
    UpstreamInvalidError,
    UpstreamUnavailableError,
)
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.contracts import (
    ResolvedPrincipal,
    TrustedCredential,
)

_LOGGER = get_logger("factory_agent.data_api.token_gateway")

#: MES token-exchange endpoint path (not a credential).
TOKEN_PATH = "/api/system/token"  # nosec B105 - path constant, not a secret


def current_bundle() -> MesCredentialBundle | None:
    """The context-bound bundle, or None outside an interaction binding."""
    return CURRENT_BUNDLE.get()


@dataclass(frozen=True, slots=True)
class LiveEntry:
    principal: ResolvedPrincipal
    bundle: MesCredentialBundle
    #: Retained exclusively for refresh re-exchange; never logged or exposed.
    encrypted_credential: str
    exchanged_at: datetime


class TokenCredentialExchange:
    """Exchanges encrypted credentials for bundles and binds them per caller.

    Implements the ``CredentialExchange`` and ``CredentialBinder`` ports. A
    single process-level httpx pool serves all exchanges; timeouts are bounded
    and failures degrade to structured unauthenticated/unavailable errors.
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        clock: Any | None = None,
        refresh_threshold_seconds: int = 5400,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._refresh_threshold_seconds = refresh_threshold_seconds
        self._entries_by_identity: dict[tuple[str, str], LiveEntry] = {}
        self._entries_by_digest: dict[str, LiveEntry] = {}

    # ------------------------------------------------------------ exchange

    async def authenticate(self, encrypted_credential: str) -> ResolvedPrincipal:
        """Exchange the encrypted app_key; reuse a fresh cached bundle."""
        if not encrypted_credential or not encrypted_credential.strip():
            raise UnauthenticatedError("credential is empty")
        digest = digest_of(encrypted_credential)
        cached = self._entries_by_digest.get(digest)
        now = self._now()
        if cached is not None and not self._needs_exchange(cached.bundle, now):
            return cached.principal
        bundle = await self._exchange(encrypted_credential)
        principal = _principal_from_bundle(bundle)
        entry = LiveEntry(
            principal=principal,
            bundle=bundle,
            encrypted_credential=encrypted_credential,
            exchanged_at=now,
        )
        self._entries_by_digest[digest] = entry
        self._entries_by_identity[_identity_key(principal.credential)] = entry
        _LOGGER.info(
            "mes.token.exchanged",
            tenant_id=str(principal.credential.tenant_id),
            role=principal.role.value,
        )
        return principal

    def principal_for(self, credential: TrustedCredential) -> ResolvedPrincipal | None:
        entry = self._entries_by_identity.get(_identity_key(credential))
        return entry.principal if entry is not None else None

    def bundle_for(self, credential: TrustedCredential) -> MesCredentialBundle | None:
        entry = self._entries_by_identity.get(_identity_key(credential))
        return entry.bundle if entry is not None else None

    # ------------------------------------------------------------- binding

    @contextlib.contextmanager
    def bind_for(self, credential: TrustedCredential) -> Generator[None, None, None]:
        """Bind the caller's live bundle for the duration of one interaction."""
        entry = self._entries_by_identity.get(_identity_key(credential))
        if entry is None:
            raise UnauthenticatedError("credential has not been exchanged")
        token = CURRENT_BUNDLE.set(entry.bundle)
        try:
            yield
        finally:
            CURRENT_BUNDLE.reset(token)

    # ------------------------------------------------------------- refresh

    async def refresh_bundle(self, tenant_id: TenantId, user_id: UserId) -> MesCredentialBundle:
        """Re-exchange the stored encrypted credential for one caller."""
        entry = self._entries_by_identity.get((str(tenant_id), str(user_id)))
        if entry is None:
            raise UnauthenticatedError("no stored credential for this caller")
        bundle = await self._exchange(entry.encrypted_credential)
        principal = _principal_from_bundle(bundle)
        refreshed = replace(entry, principal=principal, bundle=bundle, exchanged_at=self._now())
        self._entries_by_identity[(str(tenant_id), str(user_id))] = refreshed
        self._entries_by_digest[digest_of(entry.encrypted_credential)] = refreshed
        if CURRENT_BUNDLE.get() is entry.bundle:
            CURRENT_BUNDLE.set(bundle)
        _LOGGER.info(
            "mes.token.refreshed",
            tenant_id=str(tenant_id),
            role=principal.role.value,
        )
        return bundle

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- helpers

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    def _needs_exchange(self, bundle: MesCredentialBundle, now: datetime) -> bool:
        return bundle.needs_refresh(now, self._refresh_threshold_seconds)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout_seconds)
        return self._client

    async def _exchange(self, encrypted_credential: str) -> MesCredentialBundle:
        try:
            response = await self._ensure_client().post(
                TOKEN_PATH, json={"app_key": encrypted_credential}
            )
        except httpx.HTTPError:
            raise UpstreamUnavailableError("token endpoint is unreachable") from None
        if response.status_code != 200:
            raise UpstreamUnavailableError("token endpoint rejected the exchange")
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamInvalidError("token response is not valid JSON") from error
        try:
            envelope = MesEnvelope.model_validate(payload)
        except Exception as error:  # noqa: BLE001 - pydantic ValidationError
            raise UpstreamInvalidError("token envelope failed schema validation") from error
        if envelope.code != 1:
            _LOGGER.warning("mes.token.rejected", category="exchange_failed")
            raise map_message_to_error(envelope.message)
        result_payload = cast("dict[str, Any] | None", envelope.result)
        if not isinstance(result_payload, dict):
            raise UpstreamInvalidError("token result has an unexpected shape")
        try:
            result = CredentialBundleResponse.model_validate(result_payload)
        except MesError:
            raise
        except Exception as error:  # noqa: BLE001 - pydantic ValidationError
            raise UpstreamInvalidError("token result failed schema validation") from error
        return _bundle_from_response(result, self._now())


class GatewayTokenRefresher:
    """``TokenRefresher`` refreshing the context-bound caller's bundle."""

    def __init__(self, gateway: TokenCredentialExchange) -> None:
        self._gateway = gateway

    async def refresh(self) -> MesCredentialBundle:
        bundle = CURRENT_BUNDLE.get()
        if bundle is None:
            raise UnauthenticatedError("no bound credential to refresh")
        return await self._gateway.refresh_bundle(bundle.tenant_id, bundle.user)


class TokenBackedMembershipResolver:
    """MembershipSource over the exchange: membership is the token principal.

    Zero business-data calls happen here: role and bound departments are the
    authoritative token fields, so authorization completes before any MES
    request (see ``docs/product/需求及方案整理.md``「客户确认结论」).
    """

    def __init__(self, gateway: TokenCredentialExchange) -> None:
        self._gateway = gateway

    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership:
        principal = self._gateway.principal_for(credential)
        if principal is None:
            raise LookupError("credential has not been exchanged")
        return TenantMembership(
            user_id=principal.credential.user_id,
            tenant_id=principal.credential.tenant_id,
            employee_id=EmployeeId(str(principal.credential.user_id)),
            display_name=principal.display_name,
            role=principal.role,
            bound_dept_ids=principal.bound_dept_ids,
        )


def _principal_from_bundle(bundle: MesCredentialBundle) -> ResolvedPrincipal:
    if not bundle.roles:
        raise UnauthenticatedError("token response carries no role")
    try:
        role = Role.from_mes_code(bundle.roles[0])
    except ValueError as error:
        raise UnauthenticatedError("token response carries an unknown role") from error
    bound = tuple(DeptId(code) for code in bundle.effective_bound_depts if code and code.strip())
    return ResolvedPrincipal(
        credential=TrustedCredential(tenant_id=bundle.tenant_id, user_id=bundle.user),
        display_name=bundle.uname,
        role=role,
        bound_dept_ids=bound,
    )


def _bound_dept_codes(result: CredentialBundleResponse) -> tuple[str, ...]:
    """Resolve the bound-department set across both MES shapes.

    Live customer environment (2026-09-04 联调): multi-dept bindings arrive as
    the comma-separated ``manageDept`` string (role 02 → ``"001,005"`` while
    ``dept`` stays ``"001"``). Mock-era deployments emit the ``boundDepts``
    array instead. ``manageDept`` is authoritative when non-empty; the array
    form is the fallback so neither shape silently narrows a manager's scope.
    """
    from_manage = tuple(part.strip() for part in result.manageDept.split(",") if part.strip())
    if from_manage:
        return from_manage
    return tuple(result.boundDepts)


def _bundle_from_response(result: CredentialBundleResponse, now: datetime) -> MesCredentialBundle:
    expires_at = _parse_expiry(result.expiresAt, result.expiresIn, now)
    return MesCredentialBundle(
        access_token=result.accessToken,
        app_key=result.appkey,
        sign=result.sign,
        timestamp=result.timestamp,
        expires_at=expires_at,
        user=UserId(result.user),
        uname=result.uname,
        roles=tuple(result.roles),
        permissions=tuple(result.permissions),
        dept=result.dept,
        bound_depts=_bound_dept_codes(result),
    )


def _parse_expiry(raw: str, expires_in: int, now: datetime) -> datetime:
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return now + timedelta(seconds=max(expires_in, 0))


def _identity_key(credential: TrustedCredential) -> tuple[str, str]:
    return (str(credential.tenant_id), str(credential.user_id))


def digest_of(encrypted_credential: str) -> str:
    """Irreversible cache key; the encrypted value itself is never stored."""
    return hashlib.sha256(encrypted_credential.encode("utf-8")).hexdigest()


__all__ = [
    "CURRENT_BUNDLE",
    "GatewayTokenRefresher",
    "TokenBackedMembershipResolver",
    "TokenCredentialExchange",
    "current_bundle",
]
