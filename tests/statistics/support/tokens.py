"""Bearer-token helpers for statistics tests.

Platform tokens are HMAC-signed by ``AuthService`` (D15); tests need tokens for
each role without going through the bcrypt login round trip. This helper builds
the same wire format from the public ``sign_token`` helper rather than reaching
into private members, so a token-format change still breaks the tests loudly.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

from factory_agent.statistics.platform_auth import sign_token


def issue_platform_token(
    secret: str,
    *,
    role: str = "admin",
    principal_id: str = "ops-1",
    tenant_ids: tuple[str, ...] = (),
    ttl_seconds: int = 3600,
) -> str:
    expires = int((datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).timestamp())
    payload: dict[str, object] = {
        "sub": principal_id,
        "role": role,
        "tenants": list(tenant_ids),
        "exp": expires,
    }
    body = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    return f"{body}.{sign_token(secret, body)}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


__all__ = ["bearer", "issue_platform_token"]
