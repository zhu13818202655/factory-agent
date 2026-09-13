"""Audit event baseline: query, api_call, export, download events.

Events carry only whitelisted fields and irreversible scope digests; raw
employee/dept ID lists, row data, and sensitive values never enter audit.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from factory_agent.observability.logging_adapter import get_logger


class AuditEventType(StrEnum):
    QUERY = "query"
    API_CALL = "api_call"
    EXPORT = "export"
    DOWNLOAD = "download"
    #: Role-consistency safety net: an exact scope mismatch blocked a
    #: result from display in production/strict mode. Emitted as the real-time
    #: alert carrier alongside the structured log and the review table.
    SCOPE_VIOLATION_EXACT = "scope_violation_exact"
    #: Heuristic mismatch recorded without blocking (production mode).
    SCOPE_VIOLATION_HEURISTIC = "scope_violation_heuristic"


class AuditOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    FAILED = "failed"


_AUDIT_FIELD_WHITELIST: frozenset[str] = frozenset(
    {
        "event_type",
        "outcome",
        "capability_id",
        "intent_summary",
        "scope_fingerprint",
        "employee_count",
        "dept_count",
        "whole_tenant",
        "tenant_id",
        "status",
        "occurred_at",
        "request_id",
    }
)


def scope_fingerprint(
    tenant_id: str,
    employee_ids: tuple[str, ...] | None,
    dept_ids: tuple[str, ...] | None,
) -> str:
    """Irreversible digest of the effective scope; never the raw ID lists."""
    digest_input = "|".join(
        (
            tenant_id,
            ",".join(sorted(employee_ids)) if employee_ids is not None else "*",
            ",".join(sorted(dept_ids)) if dept_ids is not None else "*",
        )
    )
    return hashlib.sha256(digest_input.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_type: AuditEventType
    outcome: AuditOutcome
    capability_id: str | None
    intent_summary: str | None
    scope_fingerprint: str | None
    employee_count: int | None
    dept_count: int | None
    whole_tenant: bool
    tenant_id: str | None
    status: str
    occurred_at: datetime
    request_id: str

    def to_payload(self) -> dict[str, object]:
        """Whitelisted projection safe for persistence and tests."""
        payload: dict[str, object] = {}
        for field_name in _AUDIT_FIELD_WHITELIST:
            value = getattr(self, field_name)
            if value is not None:
                payload[field_name] = value.value if isinstance(value, StrEnum) else value
        return payload


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class AuditWriteError(RuntimeError):
    """Raised when an audit write fails; DEC-014 defaults to denying the request."""


class InMemoryAuditSink:
    """Test/offline implementation used when no persistent sink is wired."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class StructuredLogAuditSink:
    """Durable audit sink emitting one redacted structured record per event.

    The process owns no audit table, so retention and querying belong to
    whatever collects the service stdout — the sink decides no schema and no
    retention window of its own. Every field goes out through the whitelisted
    ``AuditEvent.to_payload`` projection and the logging adapter's redaction,
    so the emitted record carries the same guarantee as the in-memory one.

    An emission failure raises ``AuditWriteError``; whether that denies the
    audited action or only alerts is the caller's decision.
    """

    def __init__(self, component: str = "audit") -> None:
        self._logger = get_logger(component)

    async def record(self, event: AuditEvent) -> None:
        payload = event.to_payload()
        event_name = str(payload.pop("event_type"))
        try:
            # The event rides in one namespaced field so it can never shadow a
            # correlation field the log context contributes — ``request_id``
            # here means the inbound request, not the audited artifact.
            self._logger.info(f"audit.{event_name}", audit=payload)
        except Exception as error:  # noqa: BLE001 - any sink failure is an audit failure
            raise AuditWriteError("audit event could not be emitted") from error


__all__ = [
    "AuditEvent",
    "AuditEventType",
    "AuditOutcome",
    "AuditSink",
    "AuditWriteError",
    "InMemoryAuditSink",
    "StructuredLogAuditSink",
    "scope_fingerprint",
]
