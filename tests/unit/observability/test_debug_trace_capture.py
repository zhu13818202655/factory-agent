"""B-channel capture: what is kept, what is withheld, and what is bounded.

The debug channel has its own redaction rule, and it is the *opposite* instinct
from the log policy: business content is kept, credential material is withheld.
These tests exist to make that trade explicit and to fail loudly if it is ever
inverted — a channel that starts withholding `prompt` stores nothing useful, and
a channel that starts keeping `sign` is a credential leak with an expiry date.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from factory_agent.observability.debug_trace import (
    CaptureScope,
    close_capture_scope,
    configure_debug_trace,
    debug_capture_enabled,
    drain_debug_captures,
    llm_span_key,
    mes_span_key,
    open_capture_scope,
    record_debug_capture,
    summarise_payload,
)
from tests.support.payload import as_dict, as_list

NOW = datetime(2026, 9, 22, 0, 20, 31, tzinfo=timezone.utc)
SCOPE = CaptureScope(
    tenant_id="tenant-a",
    user_id="user-a",
    session_id="session-1",
    interaction_id="interaction-1",
)

CANARY_APP_KEY = "APPKEY-SECRET-9f3a"
CANARY_SIGN = "sign-9f3a2c88deadbeef"
CANARY_TOKEN = "access-token-9f3a2c88"
CANARIES = (CANARY_APP_KEY, CANARY_SIGN, CANARY_TOKEN)

#: Business content the debug channel is *supposed* to retain. Kept separate
#: from the canaries so the two assertions cannot be satisfied by one rule.
BUSINESS = ("上月工资是多少", "张三", "8213.44")


@pytest.fixture(autouse=True)
def _reset_capture_state() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Leave the module-level gate closed after every test.

    The gate is process state, so a test that opens it and forgets to close it
    would silently hand capture to the next test in the session.
    """
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()
    yield
    configure_debug_trace(enabled=False, max_payload_bytes=262_144, max_rows=500)
    close_capture_scope()


def _enable(*, max_payload_bytes: int = 262_144, max_rows: int = 500) -> None:
    configure_debug_trace(enabled=True, max_payload_bytes=max_payload_bytes, max_rows=max_rows)


def test_capture_is_closed_until_it_is_configured() -> None:
    """The default is "no capability", not "a switch that happens to be off"."""
    assert debug_capture_enabled() is False

    open_capture_scope(SCOPE)
    record_debug_capture(
        span_key="mes:SystemToken:1",
        kind="mes",
        input_payload={"a": 1},
        output_payload={"b": 2},
    )

    assert drain_debug_captures() == ()


def test_capture_outside_a_scope_is_dropped_without_raising() -> None:
    """A health probe calling the adapter directly must not capture anything."""
    _enable()

    record_debug_capture(
        span_key="mes:SystemToken:1",
        kind="mes",
        input_payload={"a": 1},
        output_payload={"b": 2},
    )

    assert drain_debug_captures() == ()


def test_credential_material_is_withheld_while_business_content_is_kept() -> None:
    """The channel's whole trade, asserted in one place."""
    payload = summarise_payload(
        {
            "app_key": CANARY_APP_KEY,
            "sign": CANARY_SIGN,
            "accessToken": CANARY_TOKEN,
            "password": "hunter2",
            "Cookie": "session=abc",
            "ts": 1,
            "dh": "SO-2026-0001",
        },
        {"prompt": list(BUSINESS), "amount": "8213.44"},
        max_payload_bytes=262_144,
        max_rows=500,
    )

    serialized = json.dumps(payload.input, ensure_ascii=False, default=str) + json.dumps(
        payload.output, ensure_ascii=False, default=str
    )
    for canary in CANARIES:
        assert canary not in serialized
    assert "hunter2" not in serialized
    assert "session=abc" not in serialized
    # Business values survive: withholding them would make the channel useless.
    for value in (*BUSINESS, "SO-2026-0001", "8213.44"):
        assert value in serialized
    assert payload.truncated is False


def test_llm_usage_counters_are_kept_not_redacted() -> None:
    """``token`` substring matching must not eat token *counts*.

    ``prompt_tokens`` & co. are usage numbers, not credentials — redacting them
    makes the LLM capture row useless for cost/latency analysis. A credential
    shaped like ``refresh_token`` is still withheld.
    """
    usage = {
        "prompt_tokens": 31,
        "completion_tokens": 128,
        "cached_tokens": 0,
        "reasoning_tokens": 12,
        "total_tokens": 159,
        "refresh_token": CANARY_TOKEN,
    }
    payload = summarise_payload(
        usage,
        {"usage": usage},
        max_payload_bytes=262_144,
        max_rows=500,
    )

    serialized = json.dumps(payload.input, ensure_ascii=False) + json.dumps(
        payload.output, ensure_ascii=False
    )
    assert "31" in serialized
    assert "128" in serialized
    assert "159" in serialized
    assert CANARY_TOKEN not in serialized


def test_credential_shaped_values_inside_free_text_are_withheld() -> None:
    """Key matching alone is not enough: a signed URL carries its secret in text."""
    payload = summarise_payload(
        {"url": f"https://mes.invalid/api/x?app_key={CANARY_APP_KEY}&sign=deadbeef"},
        f"Authorization: Bearer {CANARY_TOKEN}",
        max_payload_bytes=262_144,
        max_rows=500,
    )

    serialized = json.dumps(payload.input, ensure_ascii=False) + json.dumps(
        payload.output, ensure_ascii=False
    )
    for canary in CANARIES:
        assert canary not in serialized
    assert "deadbeef" not in serialized
    # The path survives — it is the operation's identity, not a business value.
    assert "https://mes.invalid/api/x" in serialized


def test_rows_over_the_cap_degrade_to_structure_and_flag_truncation() -> None:
    """Over the cap the detail is dropped and *said to be* dropped."""
    rows = [{"order": f"SO-{index:05d}", "qty": index} for index in range(50)]

    payload = summarise_payload({"rows": rows}, None, max_payload_bytes=262_144, max_rows=10)

    assert payload.truncated is True
    assert payload.original_rows == 50
    assert payload.original_bytes is not None and payload.original_bytes > 0
    # Structure is preserved: the count is visible, the sample row is visible.
    # ``CapturedPayload.input`` is typed ``object`` so no producer can reach into
    # it without acknowledging the shape; the narrowing helpers do that.
    first = as_dict(as_list(as_dict(payload.input)["rows"])[0])
    assert first["_truncated_items"] == 50
    assert first["_sample"] == [{"order": "SO-00000", "qty": 0}]


def test_bytes_over_the_cap_degrade_to_structure_and_flag_truncation() -> None:
    payload = summarise_payload(
        {"blob": "x" * 5_000},
        None,
        max_payload_bytes=1_024,
        max_rows=500,
    )

    assert payload.truncated is True
    assert payload.original_bytes is not None and payload.original_bytes > 1_024
    # A single runaway field is cut rather than allowed to crowd out the shape.
    blob = as_dict(payload.input)["blob"]
    assert isinstance(blob, str)
    assert blob.endswith("…")
    assert len(blob) < 5_000


def test_an_empty_payload_is_not_reported_as_truncated() -> None:
    """``truncated`` must mean "something was removed", not "nothing was there".

    ``original_rows`` is null whenever nothing was removed — it is only
    meaningful as a record of what truncation discarded. ``original_bytes`` is
    measured either way, because the size is useful on its own.
    """
    payload = summarise_payload(None, None, max_payload_bytes=1_024, max_rows=10)

    assert payload.truncated is False
    assert payload.original_rows is None
    assert payload.original_bytes == 8
    assert payload.input is None and payload.output is None


def test_captures_carry_the_scope_identity_and_drain_once() -> None:
    _enable()
    open_capture_scope(SCOPE)

    record_debug_capture(
        span_key=mes_span_key("YskQuery", NOW),
        kind="mes",
        input_payload={"dh": "SO-1"},
        output_payload={"rows": 1},
        occurred_at=NOW,
        operation_id="YskQuery",
    )
    captured = drain_debug_captures()

    assert len(captured) == 1
    assert captured[0].tenant_id == "tenant-a"
    assert captured[0].user_id == "user-a"
    assert captured[0].interaction_id == "interaction-1"
    assert captured[0].operation_id == "YskQuery"
    # Drained, not merely read: a second drain must not write the row twice.
    assert drain_debug_captures() == ()


def test_the_buffer_survives_a_drain_so_late_captures_still_land() -> None:
    """Mirrors the MES event buffer: draining does not close the window."""
    _enable()
    open_capture_scope(SCOPE)

    record_debug_capture(
        span_key="mes:A:1", kind="mes", input_payload=None, output_payload={"n": 1}
    )
    assert len(drain_debug_captures()) == 1
    record_debug_capture(
        span_key="mes:B:1", kind="mes", input_payload=None, output_payload={"n": 2}
    )

    late = drain_debug_captures()
    assert [item.span_key for item in late] == ["mes:B:1"]


def test_closing_the_scope_discards_undrained_captures() -> None:
    _enable()
    open_capture_scope(SCOPE)
    record_debug_capture(
        span_key="mes:A:1", kind="mes", input_payload=None, output_payload={"n": 1}
    )

    close_capture_scope()

    assert drain_debug_captures() == ()


def test_span_keys_are_deterministic_so_a_fact_row_can_find_its_payload() -> None:
    """Both writers derive the key from the same reading, so they agree.

    This is the join's entire basis: the fact row is written by the metering
    path and the payload by the adapter, and neither can see the other's id.
    """
    assert mes_span_key("YskQuery", NOW) == mes_span_key("YskQuery", NOW)
    assert mes_span_key("YskQuery", NOW) != mes_span_key("YskQuery", NOW.replace(second=1))
    assert llm_span_key("lc_8f21") == "llm:lc_8f21"


def test_capture_never_raises_on_a_value_that_cannot_be_serialised() -> None:
    """A capture fault must not travel into the call it is describing."""

    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    _enable()
    open_capture_scope(SCOPE)

    record_debug_capture(
        span_key="mes:A:1",
        kind="mes",
        input_payload={"handle": _Opaque()},
        output_payload={"when": NOW},
    )
    captured = drain_debug_captures()

    assert len(captured) == 1
    serialized = json.dumps(captured[0].payload.input, ensure_ascii=False, default=str)
    assert "opaque" in serialized
