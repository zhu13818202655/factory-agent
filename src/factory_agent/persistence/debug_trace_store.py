"""B-channel debug payload store (§4.4 route B).

Separate from the metering write on purpose. The metering chain is the A
channel: it is written on every interaction in every environment and carries no
business content. This store holds the content, is written only when the
environment ceiling and the runtime switch both allow it, and expires its own
rows on its own clock.

Retention follows the ``agent_favorite`` / ``usage_export`` precedent already in
this codebase: an ``expires_at`` column plus lazy cleanup, so no new
infrastructure is introduced for a debugging aid.

Failure isolation matches ``SqlMeteringStore``: a write fault is alerted and
dropped, never raised, because a debug aid must not be able to fail the
interaction it is describing.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.observability.debug_trace import DebugTraceCapture
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.persistence.tables import debug_trace_table
from factory_agent.ports.trace import TraceDebugPayload

_LOGGER = get_logger("factory_agent.persistence.debug_trace")


class SqlDebugTraceStore:
    """Owns every read and write of the ``debug_trace`` table."""

    def __init__(self, engine: AsyncEngine, *, retention_hours: int = 24) -> None:
        self._engine = engine
        self._retention = timedelta(hours=max(1, retention_hours))

    async def write(self, captures: Sequence[DebugTraceCapture]) -> None:
        """Persist drained captures. Never raises."""
        if not captures:
            return
        try:
            async with self._engine.begin() as connection:
                for capture in captures:
                    await connection.execute(
                        pg_insert(debug_trace_table)
                        .values(_row(capture, self._retention))
                        .on_conflict_do_nothing(index_elements=["trace_id"])
                    )
        except Exception:  # noqa: BLE001 - isolation contract
            _LOGGER.exception("debug_trace.write_failed", capture_count=len(captures))

    async def purge_expired(self, now: datetime) -> int:
        """Drop rows past their retention window; returns the row count.

        Never raises: a failed sweep is alerted and retried on the next pass,
        because an uncleaned debug row is a retention smell, not an incident.
        """
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(
                    sa.delete(debug_trace_table).where(debug_trace_table.c.expires_at < now)
                )
                return int(result.rowcount or 0)
        except Exception:  # noqa: BLE001 - maintenance must never block startup
            _LOGGER.exception("debug_trace.purge_failed")
            return 0

    async def load_payloads(
        self,
        *,
        tenant_id: str,
        user_id: str,
        interaction_id: str,
        now: datetime,
    ) -> dict[str, TraceDebugPayload]:
        """Load the unexpired payloads of one interaction, keyed by span.

        The ownership pair is part of the predicate, matching
        ``agent_interaction``: a caller can only ever reach a payload it already
        proved it owns. An expired row is filtered rather than deleted here, so
        a read never doubles as a write.
        """
        statement = (
            sa.select(
                debug_trace_table.c.span_key,
                debug_trace_table.c.input_payload,
                debug_trace_table.c.output_payload,
                debug_trace_table.c.truncated,
                debug_trace_table.c.original_rows,
                debug_trace_table.c.original_bytes,
            )
            .where(
                debug_trace_table.c.tenant_id == tenant_id,
                debug_trace_table.c.user_id == user_id,
                debug_trace_table.c.interaction_id == interaction_id,
                debug_trace_table.c.expires_at >= now,
            )
            .order_by(debug_trace_table.c.created_at, debug_trace_table.c.trace_id)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return {
            str(row["span_key"]): TraceDebugPayload(
                span_key=str(row["span_key"]),
                input=row["input_payload"],
                output=row["output_payload"],
                truncated=bool(row["truncated"]),
                original_rows=_optional_int(row["original_rows"]),
                original_bytes=_optional_int(row["original_bytes"]),
            )
            for row in rows
        }


def _row(capture: DebugTraceCapture, retention: timedelta) -> dict[str, object]:
    return {
        "trace_id": f"{capture.interaction_id}:{capture.span_key}",
        "tenant_id": capture.tenant_id,
        "user_id": capture.user_id,
        "session_id": capture.session_id,
        "interaction_id": capture.interaction_id,
        "span_key": capture.span_key,
        "kind": capture.kind,
        "stage": capture.stage,
        "logical_call_id": capture.logical_call_id,
        "attempt": capture.attempt,
        "operation_id": capture.operation_id,
        "input_payload": capture.payload.input,
        "output_payload": capture.payload.output,
        "truncated": capture.payload.truncated,
        "original_rows": capture.payload.original_rows,
        "original_bytes": capture.payload.original_bytes,
        "created_at": capture.occurred_at,
        "expires_at": capture.occurred_at + retention,
    }


def _optional_int(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None


__all__ = ["SqlDebugTraceStore"]
