"""Centralized sensitive-field redaction aligned with SECURITY.md."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, cast

REDACTED = "[REDACTED]"

SENSITIVE_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"password",
        r"passwd",
        r"secret",
        r"token",
        r"api[_-]?key",
        r"authorization",
        r"bearer",
        r"cookie",
        r"credential",
        r"postgres_url",
        r"redis_url",
        r"dsn",
        r"display_name",
        r"employee_number",
        r"salary",
        r"gross_amount",
        r"unit_rate",
        r"amount",
        r"payroll",
        r"wage",
        r"completed_quantity",
        r"qualified_quantity",
        r"defective_quantity",
        r"ordered_quantity",
        r"planned_quantity",
        r"prompt",
        r"question",
        r"answer",
        r"messages",
        r"employee_ids",
        r"dept_ids",
    )
)

# Value-level patterns for strings that may embed secrets regardless of key.
_DSN_PATTERN = re.compile(r"\b(?:postgres(?:ql)?|redis|mysql)://\S+", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
#: Query strings carry business values — order numbers, employee numbers, date
#: windows — into places ADR-0004 forbids them, most notably MES request URLs.
#: Scheme, host and path are kept because together they are the operation's
#: identity; only everything after ``?`` is replaced.
_URL_QUERY_PATTERN = re.compile(r"(\bhttps?://[^\s\"'?]*)\?[^\s\"']*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Field-level redaction rules; extend only with human approval."""

    sensitive_keys: tuple[str, ...] = tuple(pattern.pattern for pattern in SENSITIVE_KEY_PATTERNS)


def is_sensitive_key(key: str) -> bool:
    return any(pattern.fullmatch(key) or pattern.search(key) for pattern in SENSITIVE_KEY_PATTERNS)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        text = _DSN_PATTERN.sub(REDACTED, value)
        return _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)
    return REDACTED


def _as_str_dict(value: Any) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()}


def redact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the mapping with sensitive keys and values redacted."""
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if is_sensitive_key(str(key)):
            redacted[key] = REDACTED
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(_as_str_dict(value))
        elif isinstance(value, list):
            items: list[Any] = []
            sequence: list[Any] = cast(list[Any], value)
            for raw_item in sequence:
                if isinstance(raw_item, dict):
                    nested: dict[str, Any] = _as_str_dict(raw_item)
                    items.append(redact_mapping(nested))
                else:
                    items.append(raw_item)
            redacted[key] = items
        elif isinstance(value, str):
            redacted[key] = redact_value(value)
        else:
            redacted[key] = value
    return redacted


def redact_text(text: str) -> str:
    """Redact credential-like substrings and URL query strings in free text.

    Applied to every rendered message, because the leak path that matters is a
    value interpolated *into* the message template: by then the structured
    ``extra`` mapping has already been left behind, so the key-based policy in
    :func:`redact_mapping` cannot see it.
    """
    without_dsn = _DSN_PATTERN.sub(REDACTED, text)
    without_bearer = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", without_dsn)
    return redact_url_query(without_bearer)


def redact_url_query(text: str) -> str:
    """Replace URL query strings with a marker, keeping scheme, host and path."""
    return _URL_QUERY_PATTERN.sub(rf"\1?{REDACTED}", text)


def text_digest(text: str) -> str:
    """Irreversible short digest of free text, for correlation without retention.

    Mirrors :func:`factory_agent.observability.audit.scope_fingerprint`: enough
    to tell whether two records describe the same answer, never enough to
    recover it.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
