import ast
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.token_gateway import TokenCredentialExchange, current_bundle
from factory_agent.domain import Role
from factory_agent.observability.context import bind_tenant_id, current_tenant_id
from factory_agent.observability.logging_adapter import configure_logging

APP_KEY_CANARY = "synthetic-token-appkey-canary"
ENCRYPTED_CANARY = "synthetic-token-encrypted-canary"
ACCESS_CANARY = "synthetic-token-access-canary"
SIGN_CANARY = "synthetic-token-sign-canary"
USER_CANARY = "synthetic-token-user-canary"
NAME_CANARY = "synthetic-token-name-canary"
NOW = datetime(2026, 10, 12, 6, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_log_context", [False, True])
async def test_actual_token_exchange_and_refresh_json_logs_hash_tenant_and_hide_credentials(
    capsys: pytest.CaptureFixture[str], bound_log_context: bool
) -> None:
    configure_logging(FactoryAgentSettings(environment="prod", log_format="json"))
    previous_tenant = current_tenant_id()
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/api/system/token"
        return httpx.Response(
            200,
            json={
                "code": 1,
                "message": "success",
                "timestamp": 1,
                "result": {
                    "tokenType": "Bearer",
                    "accessToken": ACCESS_CANARY,
                    "expiresIn": 7200,
                    "expiresAt": (NOW + timedelta(hours=2)).isoformat(),
                    "user": USER_CANARY,
                    "uname": NAME_CANARY,
                    "dept": "SYNTHETIC-DEPT",
                    "appkey": APP_KEY_CANARY,
                    "sign": SIGN_CANARY,
                    "timestamp": 1,
                    "roles": ["99"],
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="http://token.invalid", transport=httpx.MockTransport(respond)
    )
    exchange = TokenCredentialExchange("http://token.invalid", client=client, clock=FixedClock())
    try:
        bind_tenant_id(APP_KEY_CANARY if bound_log_context else None)
        principal = await exchange.authenticate(ENCRYPTED_CANARY)
        assert str(principal.credential.tenant_id) == APP_KEY_CANARY
        assert principal.role is Role.OWNER
        with exchange.bind_for(principal.credential):
            refreshed = await exchange.refresh_bundle(
                principal.credential.tenant_id, principal.credential.user_id
            )
            assert str(refreshed.tenant_id) == APP_KEY_CANARY
            assert current_bundle() is refreshed
        assert calls == 2
        assert current_tenant_id() == (APP_KEY_CANARY if bound_log_context else None)
        captured = capsys.readouterr()
        output = captured.out + captured.err
        for canary in (
            APP_KEY_CANARY,
            ENCRYPTED_CANARY,
            ACCESS_CANARY,
            SIGN_CANARY,
            USER_CANARY,
            NAME_CANARY,
        ):
            assert canary not in output
        records: list[dict[str, Any]] = [
            ast.literal_eval(line) for line in captured.out.splitlines() if line.startswith("{")
        ]
        token_records = [record for record in records if record["event"].startswith("mes.token.")]
        assert [record["event"] for record in token_records] == [
            "mes.token.exchanged",
            "mes.token.refreshed",
        ]
        tenant_hash = hashlib.sha256(APP_KEY_CANARY.encode()).hexdigest()
        assert [record["tenant_id"] for record in token_records] == [tenant_hash, tenant_hash]
        assert all(record["role"] == Role.OWNER.value for record in token_records)
    finally:
        bind_tenant_id(previous_tenant)
        await exchange.aclose()
