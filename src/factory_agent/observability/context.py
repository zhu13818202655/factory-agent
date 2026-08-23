"""Request-scoped correlation context bound via contextvars (ADR-0004)."""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_HEADER_LENGTH = 128

_request_id: ContextVar[str | None] = ContextVar("factory_agent_request_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("factory_agent_tenant_id", default=None)
_interaction_id: ContextVar[str | None] = ContextVar("factory_agent_interaction_id", default=None)


def new_request_id() -> str:
    """Generate an opaque correlation ID."""
    return secrets.token_hex(16)


def accept_request_id(header_value: str | None) -> str:
    """Validate an inbound request ID header or generate a fresh opaque ID."""
    if header_value is None or len(header_value) > _MAX_HEADER_LENGTH:
        return new_request_id()
    if not _REQUEST_ID_PATTERN.fullmatch(header_value):
        return new_request_id()
    return header_value


def bind_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def bind_tenant_id(tenant_id: str | None) -> None:
    _tenant_id.set(tenant_id)


def bind_interaction_id(interaction_id: str | None) -> None:
    _interaction_id.set(interaction_id)


def current_request_id() -> str | None:
    return _request_id.get()


def current_tenant_id() -> str | None:
    return _tenant_id.get()


def current_interaction_id() -> str | None:
    return _interaction_id.get()


def current_log_context() -> dict[str, str]:
    """Correlation fields to attach to every structured log record."""
    context: dict[str, str] = {}
    request_id = _request_id.get()
    tenant_id = _tenant_id.get()
    interaction_id = _interaction_id.get()
    if request_id is not None:
        context["request_id"] = request_id
    if tenant_id is not None:
        context["tenant_id"] = tenant_id
    if interaction_id is not None:
        context["interaction_id"] = interaction_id
    return context
