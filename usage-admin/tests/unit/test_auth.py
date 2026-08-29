"""Platform account registration, login, and token resolution (D14~D16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from usage_admin.auth import AuthError, AuthService, hash_password, verify_password
from usage_admin.platform import PlatformRole, PlatformScope, PlatformScopeError
from usage_admin.store import InMemoryUsageStore

NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)

ADMIN = PlatformScope("ops-1", PlatformRole.ADMIN, frozenset())
ANALYST = PlatformScope("ops-2", PlatformRole.ANALYST, frozenset())

SIGNING_SECRET = "test-signing-secret"


def make_auth(*, api_token: str | None = None) -> tuple[AuthService, InMemoryUsageStore]:
    store = InMemoryUsageStore()
    counter = iter(range(1000))
    service = AuthService(
        store,
        clock=lambda: NOW,
        new_id=lambda: f"principal-{next(counter)}",
        signing_secret=SIGNING_SECRET,
        api_token=api_token,
        token_ttl_seconds=3600,
    )
    return service, store


def test_password_hash_round_trip() -> None:
    hashed = hash_password("s3cret-password")
    assert hashed != "s3cret-password"
    assert verify_password("s3cret-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_uses_random_salt() -> None:
    assert hash_password("same") != hash_password("same")


@pytest.mark.asyncio
async def test_register_requires_admin_role() -> None:
    service, _ = make_auth()
    with pytest.raises(PlatformScopeError, match="admin"):
        await service.register(
            ANALYST,
            username="ops-user",
            password="password-123",
            role="viewer",
            tenant_scope=(),
        )


@pytest.mark.asyncio
async def test_register_creates_hashed_principal_and_audits() -> None:
    service, store = make_auth()

    view = await service.register(
        ADMIN,
        username="ops-user",
        password="password-123",
        role="analyst",
        tenant_scope=("fac-01", "fac-02"),
    )

    assert view.role == "analyst"
    assert view.status == "active"
    principal = await store.get_principal(view.principal_id)
    assert principal is not None
    assert principal.password_hash != "password-123"
    assert verify_password("password-123", principal.password_hash)
    assert any(entry.action == "principal.register" for entry in store.audits)


@pytest.mark.asyncio
async def test_register_rejects_duplicate_username() -> None:
    service, _ = make_auth()
    await service.register(
        ADMIN, username="dup", password="password-123", role="viewer", tenant_scope=()
    )
    with pytest.raises(AuthError, match="already exists"):
        await service.register(
            ADMIN, username="dup", password="password-123", role="viewer", tenant_scope=()
        )


@pytest.mark.asyncio
async def test_register_rejects_weak_password_and_unknown_role() -> None:
    service, _ = make_auth()
    with pytest.raises(AuthError, match="8 characters"):
        await service.register(
            ADMIN, username="a", password="short", role="viewer", tenant_scope=()
        )
    with pytest.raises(AuthError, match="role"):
        await service.register(
            ADMIN, username="b", password="password-123", role="boss", tenant_scope=()
        )


@pytest.mark.asyncio
async def test_login_returns_signed_token_for_active_principal() -> None:
    service, _ = make_auth()
    await service.register(
        ADMIN, username="ops-user", password="password-123", role="admin", tenant_scope=()
    )

    token = await service.login("ops-user", "password-123")
    scope = service.resolve_token(token)

    assert scope is not None
    assert scope.role == PlatformRole.ADMIN
    assert scope.tenant_ids == frozenset()
    assert scope.principal_id.startswith("principal-")


@pytest.mark.asyncio
async def test_login_failure_is_rejected_and_audited() -> None:
    service, store = make_auth()
    await service.register(
        ADMIN, username="ops-user", password="password-123", role="viewer", tenant_scope=()
    )

    with pytest.raises(AuthError, match="invalid"):
        await service.login("ops-user", "wrong-password")

    assert any(entry.action == "principal.login_failed" for entry in store.audits)


@pytest.mark.asyncio
async def test_api_token_maps_to_platform_wide_admin_scope() -> None:
    service, _ = make_auth(api_token="frontend-token-123")

    scope = service.resolve_token("frontend-token-123")

    assert scope is not None
    assert scope.role == PlatformRole.ADMIN
    assert scope.tenant_ids == frozenset()
    assert service.resolve_token("frontend-token-124") is None


def test_expired_token_is_rejected() -> None:
    service, _ = make_auth()
    token = _signed_token(
        payload={"sub": "p-1", "role": "viewer", "tenants": [], "exp": int(NOW.timestamp() - 10)},
    )
    assert service.resolve_token(token) is None


def test_tampered_token_is_rejected() -> None:
    service, _ = make_auth()
    token = _signed_token(
        payload={
            "sub": "p-1",
            "role": "viewer",
            "tenants": [],
            "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        },
    )
    assert service.resolve_token(token[:-2] + "xx") is None


def _signed_token(*, payload: dict[str, object]) -> str:
    import base64
    import hashlib
    import hmac
    import json

    body = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(SIGNING_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"
