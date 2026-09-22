"""Read model backing the trace view (§4.6).

Reads the A-channel carriers directly. It is a *reader*: nothing here writes,
and the one place a payload enters is ``SqlDebugTraceStore``, which owns the
B-channel table.

Two properties are load-bearing and easy to lose:

* **Ownership is in the predicate.** The phase query filters on the full
  ``(tenant_id, user_id)`` pair, because ``agent_interaction_event`` carries
  both. The fact tables carry only ``tenant_id`` (they are metering rows, not
  business rows), so their reads are scoped by the interaction id *after* the
  caller has resolved that interaction through its owner — never by id alone.
* **The metering tables are read, not queried ad hoc.** Column lists are
  explicit so a schema addition cannot silently reshape the response.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from factory_agent.persistence.debug_trace_store import SqlDebugTraceStore
from factory_agent.persistence.tables import (
    event_table,
    interaction_fact_table,
    llm_call_fact_table,
    mes_call_fact_table,
)
from factory_agent.ports.trace import (
    InteractionFactRecord,
    LlmSpanRecord,
    MesSpanRecord,
    PhaseRecord,
    TraceDebugPayload,
    TraceFacts,
)

#: Event names the phase timeline is built from. ``interaction.phase`` moves the
#: state machine; ``interaction.progress`` marks a long stage without moving it
#: (the slowest work runs while the run is still ``PARSING``), so both are
#: stage evidence and are read together.
_PHASE_EVENT_NAMES = ("interaction.phase", "interaction.progress")


class SqlTraceStore:
    """Reads the timeline carriers of one interaction."""

    def __init__(self, engine: AsyncEngine, debug_trace: SqlDebugTraceStore) -> None:
        self._engine = engine
        self._debug_trace = debug_trace

    async def load(
        self,
        *,
        tenant_id: str,
        user_id: str,
        interaction_id: str,
    ) -> TraceFacts:
        return TraceFacts(
            phases=await self._load_phases(tenant_id, user_id, interaction_id),
            llm_spans=await self._load_llm_spans(tenant_id, interaction_id),
            mes_spans=await self._load_mes_spans(tenant_id, interaction_id),
            interaction_fact=await self._load_interaction_fact(tenant_id, interaction_id),
            debug_payloads=await self._load_debug_payloads(tenant_id, user_id, interaction_id),
        )

    async def _load_phases(
        self, tenant_id: str, user_id: str, interaction_id: str
    ) -> tuple[PhaseRecord, ...]:
        statement = (
            sa.select(
                event_table.c.sequence,
                event_table.c.name,
                event_table.c.data,
                event_table.c.created_at,
            )
            .where(
                event_table.c.tenant_id == tenant_id,
                event_table.c.user_id == user_id,
                event_table.c.interaction_id == interaction_id,
                event_table.c.name.in_(_PHASE_EVENT_NAMES),
            )
            .order_by(event_table.c.sequence)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        phases: list[PhaseRecord] = []
        for row in rows:
            # Narrow through ``object`` and cast: the JSON column comes back as
            # ``Any``, and a bare ``isinstance`` branch would propagate that
            # ``Any`` into every read below, defeating the point of typing the
            # row at all.
            raw: object = row["data"]
            data: Mapping[str, object] = (
                cast("Mapping[str, object]", raw) if isinstance(raw, dict) else {}
            )
            name = str(data.get("state") or row["name"])
            phases.append(
                PhaseRecord(
                    name=name,
                    label=str(data.get("stage") or name),
                    status=str(data.get("status") or "ok"),
                    sequence=int(row["sequence"]),
                    occurred_at=row["created_at"],
                )
            )
        return tuple(phases)

    async def _load_llm_spans(
        self, tenant_id: str, interaction_id: str
    ) -> tuple[LlmSpanRecord, ...]:
        statement = (
            sa.select(
                llm_call_fact_table.c.logical_call_id,
                llm_call_fact_table.c.stage,
                llm_call_fact_table.c.model_alias,
                llm_call_fact_table.c.actual_model,
                llm_call_fact_table.c.attempt,
                llm_call_fact_table.c.prompt_tokens,
                llm_call_fact_table.c.completion_tokens,
                llm_call_fact_table.c.cached_tokens,
                llm_call_fact_table.c.reasoning_tokens,
                llm_call_fact_table.c.duration_ms,
                llm_call_fact_table.c.status,
                llm_call_fact_table.c.fallback_reason,
                llm_call_fact_table.c.error_category,
                llm_call_fact_table.c.started_at,
                llm_call_fact_table.c.ended_at,
                llm_call_fact_table.c.parent_span_id,
                llm_call_fact_table.c.occurred_at,
            )
            .where(
                llm_call_fact_table.c.tenant_id == tenant_id,
                llm_call_fact_table.c.interaction_id == interaction_id,
            )
            .order_by(
                llm_call_fact_table.c.occurred_at,
                llm_call_fact_table.c.logical_call_id,
                llm_call_fact_table.c.attempt,
            )
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return tuple(
            LlmSpanRecord(
                logical_call_id=str(row["logical_call_id"]),
                stage=str(row["stage"]),
                model_alias=str(row["model_alias"]),
                actual_model=str(row["actual_model"]),
                attempt=int(row["attempt"]),
                prompt_tokens=int(row["prompt_tokens"]),
                completion_tokens=int(row["completion_tokens"]),
                cached_tokens=int(row["cached_tokens"]),
                reasoning_tokens=int(row["reasoning_tokens"]),
                duration_ms=int(row["duration_ms"]),
                status=str(row["status"]),
                fallback_reason=_optional_str(row["fallback_reason"]),
                error_category=_optional_str(row["error_category"]),
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                parent_span_id=_optional_str(row["parent_span_id"]),
                occurred_at=row["occurred_at"],
            )
            for row in rows
        )

    async def _load_mes_spans(
        self, tenant_id: str, interaction_id: str
    ) -> tuple[MesSpanRecord, ...]:
        statement = (
            sa.select(
                mes_call_fact_table.c.operation_id,
                mes_call_fact_table.c.page_count,
                mes_call_fact_table.c.row_count_bucket,
                mes_call_fact_table.c.duration_ms,
                mes_call_fact_table.c.status,
                mes_call_fact_table.c.error_category,
                mes_call_fact_table.c.started_at,
                mes_call_fact_table.c.ended_at,
                mes_call_fact_table.c.parent_span_id,
                mes_call_fact_table.c.occurred_at,
            )
            .where(
                mes_call_fact_table.c.tenant_id == tenant_id,
                mes_call_fact_table.c.interaction_id == interaction_id,
            )
            .order_by(mes_call_fact_table.c.occurred_at)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return tuple(
            MesSpanRecord(
                operation_id=str(row["operation_id"]),
                page_count=int(row["page_count"]),
                row_count_bucket=str(row["row_count_bucket"]),
                duration_ms=int(row["duration_ms"]),
                status=str(row["status"]),
                error_category=_optional_str(row["error_category"]),
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                parent_span_id=_optional_str(row["parent_span_id"]),
                occurred_at=row["occurred_at"],
            )
            for row in rows
        )

    async def _load_interaction_fact(
        self, tenant_id: str, interaction_id: str
    ) -> InteractionFactRecord | None:
        statement = (
            sa.select(
                interaction_fact_table.c.status,
                interaction_fact_table.c.duration_ms,
                interaction_fact_table.c.mes_duration_ms,
                interaction_fact_table.c.llm_duration_ms,
                interaction_fact_table.c.local_duration_ms,
                interaction_fact_table.c.result_rows_bucket,
            )
            .where(
                interaction_fact_table.c.tenant_id == tenant_id,
                interaction_fact_table.c.interaction_id == interaction_id,
                interaction_fact_table.c.event_type == "interaction_completed",
            )
            .order_by(interaction_fact_table.c.occurred_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None
        return InteractionFactRecord(
            status=_optional_str(row["status"]),
            duration_ms=_optional_int(row["duration_ms"]),
            mes_duration_ms=_optional_int(row["mes_duration_ms"]),
            llm_duration_ms=_optional_int(row["llm_duration_ms"]),
            local_duration_ms=_optional_int(row["local_duration_ms"]),
            result_rows_bucket=_optional_str(row["result_rows_bucket"]),
        )

    async def _load_debug_payloads(
        self, tenant_id: str, user_id: str, interaction_id: str
    ) -> Mapping[str, TraceDebugPayload]:
        """B-channel payloads, keyed by span; empty when capture is off.

        The lookup is unconditional rather than gated on the switch: an
        operator who turns capture off after an incident should still be able
        to read what is already stored and unexpired.
        """
        return await self._debug_trace.load_payloads(
            tenant_id=tenant_id,
            user_id=user_id,
            interaction_id=interaction_id,
            now=datetime.now(timezone.utc),
        )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None


__all__ = ["SqlTraceStore"]
