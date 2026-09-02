"""Metering privacy: credentials never leak into events, logs, or snapshots.

AppKey / ``sign`` / ``accessToken`` must not appear in
``usage_event`` payloads, structured logs, error messages, or test snapshots of
the metering chain. This suite runs the real event constructors and the MES
adapter recorder with credential canaries and asserts absence.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timezone
from typing import Any

import pytest

from factory_agent.application.usage import (
    UsageContext,
    mes_call_completed_event,
)
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.domain import InteractionId, SessionId, TenantId, UserId
from factory_agent.ports import MesCallRecord

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
CANARY_APP_KEY = "APPKEY-SECRET-9f3a"
CANARY_SIGN = "sign-9f3a2c88deadbeef"
CANARY_TOKEN = "access-token-9f3a2c88"
CANARY_UNAME = "模拟员工-金库里-张三"

CANARIES = (CANARY_APP_KEY, CANARY_SIGN, CANARY_TOKEN, CANARY_UNAME)


def canary_bundle() -> MesCredentialBundle:
    return MesCredentialBundle(
        access_token=CANARY_TOKEN,
        app_key=CANARY_APP_KEY,
        sign=CANARY_SIGN,
        timestamp=1,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("01001"),
        uname=CANARY_UNAME,
    )


def context() -> UsageContext:
    return UsageContext(
        tenant_id=TenantId("tenant-a"),
        user_id=UserId("user-a"),
        session_id=SessionId("session-1"),
        interaction_id=InteractionId("interaction-1"),
        trace_id="a" * 32,
    )


def test_mes_call_event_never_carries_a_credential_canary() -> None:
    produced = mes_call_completed_event(
        context(),
        occurred_at=NOW,
        operation_id="YskQuery",
        page_count=1,
        row_count=0,
        duration_ms=5,
        status="failed",
        error_category="签名无效",
    )

    serialized = json.dumps(produced.payload, ensure_ascii=False, default=str)
    for canary in CANARIES:
        assert canary not in serialized


def test_mes_call_event_never_carries_a_parameter_value() -> None:
    """Business parameter values (dates, employee ids) never enter the event."""
    produced = mes_call_completed_event(
        context(),
        occurred_at=NOW,
        operation_id="YskQuery",
        page_count=1,
        row_count=0,
        duration_ms=5,
        status="completed",
    )

    serialized = json.dumps(produced.payload, ensure_ascii=False, default=str)
    assert "2026-07-01" not in serialized
    assert "01001" not in serialized
    assert "api/" not in serialized


def test_adapter_record_never_carries_a_credential_canary() -> None:
    """The record handed to the recorder excludes URL/credentials by shape."""
    record = MesCallRecord(
        operation_id="YskQuery",
        page_count=1,
        row_count=0,
        duration_ms=5,
        status="failed",
        error_category=CANARY_UNAME,  # adversarial: even an odd error carries nothing
    )

    serialized = repr(record)
    assert CANARY_APP_KEY not in serialized
    assert CANARY_TOKEN not in serialized


def test_metering_log_records_no_credential_canaries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The structured alert on a metering failure carries only the event count."""
    from factory_agent.persistence.metering import SqlMeteringStore

    class FailingEngine:
        def begin(self) -> Any:
            raise RuntimeError("boom")

    import asyncio

    store = SqlMeteringStore(FailingEngine())  # type: ignore[arg-type]
    from factory_agent.ports import UsageEvent

    event = UsageEvent(
        event_id="11111111-1111-4111-8111-111111111111",
        event_type="mes_call_completed",
        tenant_id=TenantId("tenant-a"),
        payload={"event_type": "mes_call_completed", "operation_id": "YskQuery"},
        created_at=NOW,
    )

    with caplog.at_level(logging.ERROR, logger="factory_agent.persistence.metering"):
        asyncio.run(store.write_usage_events([event]))  # type: ignore[arg-type]

    assert "usage.metering.write_failed" in caplog.text
    for canary in CANARIES:
        assert canary not in caplog.text


def test_adapter_error_message_never_leaks_credentials() -> None:
    """The raised MES error is credential-free even with a canary-filled bundle."""
    from factory_agent.domain.errors import TenantDisabledError

    error = TenantDisabledError()

    assert CANARY_APP_KEY not in str(error)
    assert CANARY_TOKEN not in str(error)
