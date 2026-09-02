"""Direct-to-table metering writes with failure isolation (Story 11).

``SqlMeteringStore`` writes the raw ``usage_event`` archive plus the derived
``interaction_fact`` / ``llm_call_fact`` / ``mes_call_fact`` rows in one
transaction, using ``event_id`` + ``ON CONFLICT DO NOTHING`` for idempotency
(the ``usage_event`` primary key is ``(event_id, occurred_at)``).

Failure isolation (Story 11 1.6 / R1): every write is wrapped so that any
exception is logged as a structured alert and never raised into the business
transaction. The metering transaction is intentionally separate from the
business commit — a metering failure can therefore never roll back an answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from factory_agent.observability.logging_adapter import get_logger
from factory_agent.persistence.tables import (
    interaction_fact_table,
    llm_call_fact_table,
    mes_call_fact_table,
    usage_event_table,
)
from factory_agent.ports import UsageEvent

_LOGGER = get_logger("factory_agent.persistence.metering")

#: Event types that carry a derived fact row in the same write.
_INTERACTION_EVENT_TYPES = frozenset({"interaction_started", "interaction_completed"})
_LLM_EVENT_TYPES = frozenset({"llm_call_completed"})
_MES_EVENT_TYPES = frozenset({"mes_call_completed"})


class SqlMeteringStore:
    """Owns every metering write for this service."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self._engine = engine
        self._on_failure = on_failure

    async def write_usage_events(self, events: Sequence[UsageEvent]) -> None:
        """Write the archive row and derived facts in one transaction.

        Never raises: a metering failure is logged (and forwarded to the
        optional alert callback) without blocking the caller.
        """
        if not events:
            return
        try:
            async with self._engine.begin() as connection:
                for event in events:
                    await self._write_event(connection, event)
        except Exception as error:  # noqa: BLE001 - isolation contract (Story 11 1.6)
            _LOGGER.exception("usage.metering.write_failed", event_count=len(events))
            if self._on_failure is not None:
                try:
                    self._on_failure(error)
                except Exception:  # noqa: BLE001 - alerting must never raise
                    _LOGGER.exception("usage.metering.alert_failed")

    async def _write_event(self, connection: AsyncConnection, event: UsageEvent) -> None:
        payload = dict(event.payload)
        occurred_at = event.created_at
        received_at = occurred_at
        common = {
            "event_id": event.event_id,
            "tenant_id": str(event.tenant_id),
            "session_id": str(payload.get("session_id")),
            "interaction_id": str(payload.get("interaction_id")),
            "occurred_at": occurred_at,
            "received_at": received_at,
            "user_subject_id": str(payload.get("user_subject_id")),
        }
        await connection.execute(
            _insert_ignore(
                usage_event_table,
                {
                    **common,
                    "schema_version": str(payload.get("schema_version")),
                    "event_type": event.event_type,
                    "trace_id": str(payload.get("trace_id")),
                    "payload": payload,
                },
            )
        )
        if event.event_type in _INTERACTION_EVENT_TYPES:
            await connection.execute(
                _insert_ignore(
                    interaction_fact_table,
                    {
                        **common,
                        "event_type": event.event_type,
                        "capability_id": _nullable(payload.get("capability")),
                        "entrypoint": _nullable(payload.get("entrypoint")),
                        "role_category": _nullable(payload.get("role_category")),
                        "status": _nullable(payload.get("status")),
                        "duration_ms": _nullable_int(payload.get("duration_ms")),
                        "mes_duration_ms": _nullable_int(payload.get("mes_duration_ms")),
                        "llm_duration_ms": _nullable_int(payload.get("llm_duration_ms")),
                        "local_duration_ms": _nullable_int(payload.get("local_duration_ms")),
                        "result_rows_bucket": _nullable(payload.get("result_rows_bucket")),
                        "error_category": _nullable(payload.get("error_category")),
                    },
                )
            )
        elif event.event_type in _LLM_EVENT_TYPES:
            await connection.execute(
                _insert_ignore(
                    llm_call_fact_table,
                    _columns_for(
                        llm_call_fact_table,
                        {
                            **common,
                            "logical_call_id": str(payload.get("logical_call_id")),
                            "stage": str(payload.get("stage")),
                            "model_alias": str(payload.get("model_alias")),
                            "actual_model": str(payload.get("actual_model")),
                            "attempt": _int(payload.get("attempt")) or 1,
                            "prompt_tokens": _int(payload.get("prompt_tokens")),
                            "completion_tokens": _int(payload.get("completion_tokens")),
                            "cached_tokens": _int(payload.get("cached_tokens")),
                            "reasoning_tokens": _int(payload.get("reasoning_tokens")),
                            "duration_ms": _int(payload.get("duration_ms")),
                            "status": str(payload.get("status")),
                            "fallback_reason": _nullable(payload.get("fallback_reason")),
                            "error_category": _nullable(payload.get("error_category")),
                        },
                    ),
                )
            )
        elif event.event_type in _MES_EVENT_TYPES:
            await connection.execute(
                _insert_ignore(
                    mes_call_fact_table,
                    _columns_for(
                        mes_call_fact_table,
                        {
                            **common,
                            "operation_id": str(payload.get("operation_id")),
                            "page_count": _int(payload.get("page_count")),
                            "row_count_bucket": str(payload.get("row_count_bucket")),
                            "duration_ms": _int(payload.get("duration_ms")),
                            "status": str(payload.get("status")),
                            "error_category": _nullable(payload.get("error_category")),
                        },
                    ),
                )
            )


def _columns_for(table: sa.Table, values: dict[str, Any]) -> dict[str, Any]:
    """Keep only the columns the target table actually declares.

    The metering tables share a common envelope (``user_subject_id`` etc.), but
    ``llm_call_fact`` and ``mes_call_fact`` deliberately do not store the user
    pseudonym — passing the full ``common`` dict would raise a compile error.
    """
    names = set(table.c.keys())
    return {name: value for name, value in values.items() if name in names}


def _insert_ignore(table: sa.Table, values: dict[str, Any]) -> sa.Executable:
    """``INSERT ... ON CONFLICT DO NOTHING`` against the table's PK columns."""
    return (
        pg_insert(table)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=[column.name for column in table.primary_key.columns]
        )
    )


def _nullable(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:64] if text else None


def _nullable_int(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _int(value: object) -> int:
    if isinstance(value, (int, float, str)) and value:
        return int(value)
    return 0


__all__ = ["SqlMeteringStore"]
