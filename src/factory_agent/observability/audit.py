"""Audit event baseline: query, api_call, export, download events.

Events carry only whitelisted fields and irreversible scope digests; raw
employee/dept ID lists, row data, and sensitive values never enter audit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class AuditEventType(StrEnum):
    QUERY = "query"
    API_CALL = "api_call"
    EXPORT = "export"
    DOWNLOAD = "download"


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
    """Test/offline implementation; PostgreSQL persistence arrives in Story 4~5."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


__all__ = [
    "AuditEvent",
    "AuditEventType",
    "AuditOutcome",
    "AuditSink",
    "AuditWriteError",
    "InMemoryAuditSink",
    "scope_fingerprint",
]
