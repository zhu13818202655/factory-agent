"""Platform operations accounts and Bearer-token authentication (D14~D16).

usage-admin owns its own internal account domain (``platform_principal``),
fully isolated from factory MES users. Authentication has two channels, token
first (Technology Notes):

- ``Authorization: Bearer <token>`` — tokens are issued by ``/auth/login``
  (HMAC-signed, carrying principal_id/role/tenant_scope/expiry) or come from
  the ``USAGE_ADMIN_API_TOKEN`` setting (mapped to the ``admin`` role for
  front-end use, D16);
- the trusted-gateway three headers remain as the dev/test direct channel.

Passwords are stored hashed (bcrypt) and never logged; login failures and
account changes are recorded in ``admin_audit``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import bcrypt

from usage_admin.platform import PlatformRole, PlatformScope, PlatformScopeError
from usage_admin.store import (
    AuditEntry,
    PlatformPrincipalRecord,
    UsageStore,
)

_TOKEN_SEPARATOR = "."  # nosec B105 - token separator, not a secret literal


class AuthError(ValueError):
    """Structured rejection for registration or login failures."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()  # nosec B105 - bcrypt salt format, not a secret literal


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def sign_token(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class PrincipalView:
    principal_id: str
    username: str
    role: str
    tenant_scope: tuple[str, ...]
    status: str
    created_at: datetime
    updated_at: datetime


class AuthService:
    """Registers, logs in, and resolves platform principals."""

    def __init__(
        self,
        store: UsageStore,
        *,
        clock: Callable[[], datetime],
        new_id: Callable[[], str],
        signing_secret: str,
        api_token: str | None,
        token_ttl_seconds: int = 28_800,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id
        self._signing_secret = signing_secret
        self._api_token = api_token
        self._token_ttl_seconds = token_ttl_seconds

    @property
    def token_ttl_seconds(self) -> int:
        return self._token_ttl_seconds

    async def register(
        self,
        scope: PlatformScope,
        *,
        username: str,
        password: str,
        role: str,
        tenant_scope: tuple[str, ...],
    ) -> PrincipalView:
        """Create an operations account; only an ``admin`` may call this."""
        if not scope.allows_manage_tenants():
            raise PlatformScopeError("account registration requires the admin role")
        role_value = PlatformRole.parse(role)
        if role_value is None:
            raise AuthError(f"unsupported role {role!r}")
        normalized = username.strip()
        if not normalized:
            raise AuthError("username must not be empty")
        if len(password) < 8:
            raise AuthError("password must be at least 8 characters")
        now = self._clock()
        principal_id = self._new_id()
        record = PlatformPrincipalRecord(
            principal_id=principal_id,
            username=normalized,
            password_hash=hash_password(password),
            role=role_value.value,
            tenant_scope=tuple(sorted(set(tenant_scope))),
            status="active",
            created_at=now,
            updated_at=now,
        )
        created = await self._store.create_principal(record)
        if not created:
            raise AuthError("username already exists")
        await self._audit(
            scope,
            "principal.register",
            principal_id,
            {
                "username": normalized,
                "role": role_value.value,
                "tenant_scope": list(record.tenant_scope),
            },
        )
        return _to_view(record)

    async def login(self, username: str, password: str) -> str:
        """Return a signed Bearer token, or raise ``AuthError`` on failure."""
        principal = await self._store.get_principal_by_username(username.strip())
        if principal is None or principal.status != "active":
            await self._audit_login_failure(username)
            raise AuthError("invalid username or password")
        if not verify_password(password, principal.password_hash):
            await self._audit_login_failure(username)
            raise AuthError("invalid username or password")
        return self._issue_token(principal)

    def resolve_token(self, token: str) -> PlatformScope | None:
        """Resolve a bearer token to a ``PlatformScope``, or ``None`` when invalid.

        The configured front-end API token (D16) maps to a platform-wide admin
        scope and supports rotation by configuration change.
        """
        if not token:
            return None
        if self._api_token is not None:
            if hmac.compare_digest(token, self._api_token):
                return PlatformScope(
                    principal_id="api-token",
                    role=PlatformRole.ADMIN,
                    tenant_ids=frozenset(),
                )
        payload = self._decode_token(token)
        if payload is None:
            return None
        role = PlatformRole.parse(str(payload.get("role", "")))
        if role is None:
            return None
        tenants_raw = payload.get("tenants")
        if isinstance(tenants_raw, list):
            raw_items = cast("list[object]", tenants_raw)
            tenants = tuple(str(item) for item in raw_items if isinstance(item, str))
        else:
            tenants = ()
        return PlatformScope(
            principal_id=str(payload.get("sub", "unknown")),
            role=role,
            tenant_ids=frozenset(tenants),
        )

    def _issue_token(self, principal: PlatformPrincipalRecord) -> str:
        expires = int((self._clock() + timedelta(seconds=self._token_ttl_seconds)).timestamp())
        payload: dict[str, object] = {
            "sub": principal.principal_id,
            "role": principal.role,
            "tenants": list(principal.tenant_scope),
            "exp": expires,
        }
        body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = sign_token(self._signing_secret, body)
        return f"{body}{_TOKEN_SEPARATOR}{signature}"

    def _decode_token(self, token: str) -> dict[str, object] | None:
        parts = token.split(_TOKEN_SEPARATOR)
        if len(parts) != 2:
            return None
        body, signature = parts
        expected = sign_token(self._signing_secret, body)
        if not hmac.compare_digest(expected, signature):
            return None
        try:
            decoded = json.loads(_b64url_decode(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(decoded, dict):
            return None
        payload = cast("dict[str, object]", decoded)
        expires = payload.get("exp")
        if not isinstance(expires, (int, float)) or expires <= self._clock().timestamp():
            return None
        return payload

    async def _audit(
        self,
        scope: PlatformScope,
        action: str,
        target: str | None,
        detail: dict[str, object],
    ) -> None:
        await self._store.record_audit(
            AuditEntry(
                audit_id=self._new_id(),
                principal_id=scope.principal_id,
                action=action,
                target=target,
                detail=detail,
                created_at=self._clock(),
            )
        )

    async def _audit_login_failure(self, username: str) -> None:
        await self._store.record_audit(
            AuditEntry(
                audit_id=self._new_id(),
                principal_id=username,
                action="principal.login_failed",
                target=None,
                detail={"username": username},
                created_at=self._clock(),
            )
        )


def _to_view(record: PlatformPrincipalRecord) -> PrincipalView:
    return PrincipalView(
        principal_id=record.principal_id,
        username=record.username,
        role=record.role,
        tenant_scope=record.tenant_scope,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def generate_api_token() -> str:
    """Generate a fresh front-end API token (D16, supports rotation)."""
    return secrets.token_urlsafe(32)


__all__ = [
    "AuthError",
    "AuthService",
    "PrincipalView",
    "generate_api_token",
    "hash_password",
    "sign_token",
    "verify_password",
]
