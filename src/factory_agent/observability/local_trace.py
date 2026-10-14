import re
from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal, cast
from uuid import uuid4

from factory_agent.observability.debug_trace import (
    current_capture_scope,
    debug_capture_enabled,
    record_debug_capture,
    utc_isoformat,
)
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.observability.redaction import REDACTED, redact_text

LocalKind = Literal["cache", "duckdb"]
TimingRole = Literal["leaf", "parent"]
_LOGGER = get_logger("factory_agent.observability.local_trace")
_parent: ContextVar[str | None] = ContextVar("factory_agent_local_trace_parent", default=None)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_NUMBER_LITERAL = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


class LocalOperationTrace:
    """Best-effort observation of a local operation, without retaining rows."""

    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.input: dict[str, object] = {}
        self.output: dict[str, object] = {}
        self.status = "ok"
        self.reason: str | None = None
        self.error_category: str | None = None

    def set_output(self, **values: object) -> None:
        if self.active:
            self.output.update(values)

    def update_input(self, **values: object) -> None:
        if self.active:
            self.input.update(values)

    def set_status(self, status: str, *, reason: str | None = None) -> None:
        if self.active:
            self.status = status
            self.reason = reason


@contextmanager
def trace_local_operation(
    kind: LocalKind,
    operation: str,
    *,
    input_payload: Callable[[], object] | None = None,
    timing_role: TimingRole = "leaf",
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = perf_counter,
    new_id: Callable[[], str] | None = None,
) -> Generator[LocalOperationTrace, None, None]:
    """Record timing only in the owned, enabled developer debug channel.

    Payload factories run behind the gate. Instrumentation failures cannot
    change the operation being observed; business exceptions retain their
    original propagation and only a bounded category enters this channel.
    """
    active = debug_capture_enabled() and current_capture_scope() is not None
    trace = LocalOperationTrace(active=active)
    if not active:
        yield trace
        return
    try:
        started_at = now() if now is not None else datetime.now(timezone.utc)
        started = monotonic()
        span_id = f"{kind}:{new_id() if new_id is not None else uuid4().hex}"
        parent_id = _parent.get()
        if input_payload is not None:
            payload = input_payload()
            if isinstance(payload, Mapping):
                trace.input.update(cast(Mapping[str, object], payload))
            else:
                trace.input["value"] = payload
    except Exception:
        _LOGGER.warning("local_trace.capture_unavailable", error_category="capture_setup_failed")
        trace.active = False
        yield trace
        return
    token = _parent.set(span_id)
    try:
        yield trace
    except BaseException as error:
        if trace.status == "ok":
            trace.status = "cancelled" if type(error).__name__ == "CancelledError" else "error"
        trace.error_category = (
            trace.status if trace.status in {"cancelled", "denied"} else "operation_failed"
        )
        raise
    finally:
        _parent.reset(token)
        try:
            duration_ms = max(0, round((monotonic() - started) * 1000))
            record_debug_capture(
                span_key=span_id,
                kind=kind,
                operation_id=operation,
                stage=operation,
                input_payload=trace.input,
                output_payload={
                    "_trace": {
                        "started_at": utc_isoformat(started_at),
                        "duration_ms": duration_ms,
                        "status": trace.status,
                        "reason": trace.reason,
                        "error_category": trace.error_category,
                        "timing_role": timing_role,
                        "parent_span_id": parent_id,
                    },
                    "details": trace.output,
                },
                occurred_at=started_at,
            )
        except Exception:
            _LOGGER.warning(
                "local_trace.capture_unavailable", error_category="capture_finish_failed"
            )


def safe_sql_payload(
    sql: str,
    params: Mapping[str, object] | Sequence[object] | None = None,
) -> dict[str, object]:
    """Keep SQL structure while hiding literals, comments and parameter values.

    Binding names, types and counts explain execution without disclosing
    employee identifiers, payroll values or credentials. Parameter values are
    never copied into this diagnostic payload, including positional bindings.
    """
    safe_sql, changed = _safe_sql(sql)
    if params is None:
        bindings: object = None
    elif isinstance(params, Mapping):
        bindings = {
            name if _IDENTIFIER.fullmatch(name) else "hidden_parameter": _binding_shape(value)
            for name, value in params.items()
        }
    else:
        bindings = [_binding_shape(value) for value in params]
    return {
        "sql": safe_sql,
        "sql_redacted": changed,
        "parameters": bindings,
        "parameter_values": "hidden",
    }


def _binding_shape(value: object) -> dict[str, object]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return {
            "type": "collection",
            "count": len(cast(Collection[object], value)),
            "value": REDACTED,
        }
    kind = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "number"
        if isinstance(value, (int, float))
        else "text"
        if isinstance(value, str)
        else "bound_value"
    )
    return {"type": kind, "value": REDACTED}


def _safe_sql(sql: str) -> tuple[str, bool]:
    pieces: list[str] = []
    index = 0
    changed = False
    while index < len(sql):
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = len(sql) if end < 0 else end
            pieces.append("-- [comment hidden]")
            changed = True
        elif sql.startswith("/*", index):
            index += 2
            depth = 1
            while index < len(sql) and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            pieces.append("/* [comment hidden] */")
            changed = True
        elif sql[index] == "'":
            index = _quoted_end(sql, index, "'")
            pieces.append("'[literal hidden]'")
            changed = True
        elif sql[index] == '"':
            end = _quoted_end(sql, index, '"')
            identifier = sql[index + 1 : end - 1].replace('""', '"')
            if _IDENTIFIER.fullmatch(identifier):
                pieces.append(sql[index:end])
            else:
                pieces.append('"hidden_identifier"')
                changed = True
            index = end
        elif (match := _DOLLAR_QUOTE.match(sql, index)) is not None:
            delimiter = match.group()
            end = sql.find(delimiter, match.end())
            index = len(sql) if end < 0 else end + len(delimiter)
            pieces.append("'[literal hidden]'")
            changed = True
        elif (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] in "_$")) and (
            number := _NUMBER_LITERAL.match(sql, index)
        ) is not None:
            index = number.end()
            pieces.append("[numeric literal hidden]")
            changed = True
        else:
            pieces.append(sql[index])
            index += 1
    result = "".join(pieces)
    scrubbed = redact_text(result)
    return scrubbed, changed or scrubbed != result


def _quoted_end(text: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            if index + 1 < len(text) and text[index + 1] == quote:
                index += 2
            else:
                return index + 1
        else:
            index += 1
    return len(text)
