"""Loguru-backed logging adapter; application code never imports Loguru."""

import logging
import re
import sys
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from factory_agent.config import FactoryAgentSettings
from factory_agent.observability.context import current_log_context
from factory_agent.observability.redaction import redact_mapping, redact_text, redact_url_query

if TYPE_CHECKING:
    from loguru import Logger as LoguruLogger

_INTERCEPTED_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "httpx",
    "httpcore",
    "sqlalchemy",
    "alembic",
)

#: Loggers whose INFO records are transport-level narration: one line per
#: outbound request, stating nothing that ``mes_call_fact`` / ``llm_call_fact``
#: does not already hold in redacted, structured form — while ``httpx`` prints
#: the full request URL including its query string, which ADR-0004 forbids.
#: Pinned to ``log_third_party_level`` so connection failures and 5xx stay
#: visible while the per-request line goes.
#:
#: ``uvicorn.access`` is deliberately *not* here. uvicorn logs every request at
#: INFO whatever the status code, so a level pin would silence the 5xx that are
#: the only reason to keep an access log at all. Its volume is removed by
#: filtering (:func:`_access_is_worth_forwarding`) instead.
_NOISY_LOGGERS: frozenset[str] = frozenset({"httpx", "httpcore"})

#: Probe endpoints: infrastructure traffic, not user requests. One line per probe
#: per interval, forever, answering a question the orchestrator already reacts to
#: through the status code. Dropped outright rather than downgraded.
_QUIET_ACCESS_PATHS: tuple[str, ...] = ("/health", "/readyz", "/livez")

#: Responses below this are not worth an access line: the application already
#: records what it served, so a successful request's access entry is pure volume.
#: A failure is kept, because that is the case the access log answers something
#: the business log may not.
_ACCESS_ERROR_FLOOR = 400

#: uvicorn renders its access records as
#: ``<client> - "<METHOD> <path> HTTP/<version>" <status>``; we read the rendered
#: line rather than ``record.args`` to stay independent of uvicorn's internals.
_ACCESS_LINE = re.compile(r'"(\S+) (\S+) HTTP/\S+" (\d{3})')


def _coerce_level(raw: str) -> int:
    """Accept a level name or number; an unknown name is a startup error.

    Substituting a default would hide a typo in the one knob whose entire job is
    to control log volume.
    """
    text = raw.strip()
    if text.isdigit():
        return int(text)
    value = getattr(logging, text.upper(), None)
    if not isinstance(value, int):
        raise ValueError(f"unknown log level: {raw!r}")
    return value


def _access_is_worth_forwarding(record: logging.LogRecord) -> bool:
    """Whether a ``uvicorn.access`` record earns its line.

    An unparseable record is forwarded: a noise filter must never be the reason
    a diagnostic disappears.
    """
    match = _ACCESS_LINE.search(record.getMessage())
    if match is None:
        return True
    path, status = match.group(2), int(match.group(3))
    if path.startswith(_QUIET_ACCESS_PATHS):
        return False
    return status >= _ACCESS_ERROR_FLOOR


class _InterceptHandler(logging.Handler):
    """Forward standard logging records into the Loguru sink."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue code
        try:
            from loguru import logger as loguru_logger

            if record.name == "uvicorn.access" and not _access_is_worth_forwarding(record):
                return

            # ``origin`` is carried explicitly. Without it a forwarded record is
            # indistinguishable from an application one: ``depth`` only recovers
            # the caller frame when the stack happens to line up, and the
            # library's own name and line number are dropped outright — which is
            # what made the forwarded transport chatter impossible to attribute.
            loguru_logger.bind(origin=f"{record.name}:{record.lineno}").opt(
                depth=6, exception=record.exc_info
            ).log(record.levelname, redact_url_query(record.getMessage()))
        except Exception:
            logging.getLogger(__name__).debug("log forwarding dropped a record")


def _build_json_sink(environment: str) -> Callable[[Any], None]:
    """Build the structured one-line JSON sink used in containers.

    ``environment`` comes from settings at startup, so the sink needs no global
    state and stays reusable across tests.
    """

    def sink(message: Any) -> None:  # pragma: no cover - exercised via logging capture
        record = message.record
        from factory_agent import __version__

        payload = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "service": "factory-agent",
            "version": __version__,
            "environment": environment,
            "component": record["extra"].get("component", "app"),
            # Event names are stable identifiers, but redact anyway: a value
            # interpolated into the message must not become a leak path.
            "event": redact_text(record["message"]),
            **current_log_context(),
            **redact_mapping(
                {key: value for key, value in record["extra"].items() if key != "component"}
            ),
        }
        if record["exception"] is not None:
            # loguru wraps the original exception in a RecordException namedtuple;
            # ``.type`` is the real exception class.
            payload["error_type"] = record["exception"].type.__name__
            rendered = "".join(traceback.format_exception(*record["exception"]))
            payload["error_traceback"] = redact_text(rendered)
        sys.stdout.write(repr(payload) + "\n")

    return sink


def _console_sink(message: Any) -> None:  # pragma: no cover - local development only
    import traceback as _traceback

    record = message.record
    context = current_log_context()
    request_id = context.get("request_id", "-")
    # A forwarded record reports where it came from; an application record
    # reports its component. Locally, attribution is the whole point.
    source = record["extra"].get("origin") or record["extra"].get("component", "app")
    sys.stdout.write(
        f"{record['time'].isoformat()} | {record['level'].name:<8} | "
        f"{request_id} | {source} | "
        f"{redact_text(record['message'])}\n"
    )
    # Attach the real traceback so local debugging never hides the root cause
    # behind a structured marker (e.g. ``interaction.execution_failed``).
    if record["exception"] is not None:
        rendered = "".join(_traceback.format_exception(*record["exception"]))
        sys.stdout.write(redact_text(rendered) + "\n")


def _log_effective_environment(settings: FactoryAgentSettings) -> None:
    """State the resolved environment tier once, at startup.

    One line instead of a guess: which environment this process believes it is,
    whether content is being captured, and the caps that apply if it is. A
    second line appears only when an operator asked for content on a deployment
    that may not hold it — that request is refused, and refusing it silently
    would leave the override looking as if it had taken effect.
    """
    from loguru import logger as loguru_logger

    logger = loguru_logger.bind(component="config")
    if settings.debug_trace_enabled and not settings.is_developer_environment:
        logger.error(
            "config.debug_trace_refused env={env} requested=true applied=false "
            "reason=content_capture_not_permitted",
            env=settings.environment,
        )
    logger.info(
        "config.environment_effective env={env} content_capture={capture} "
        "debug_trace={trace} log_level={level} retention_hours={retention} "
        "max_payload_bytes={max_bytes} max_rows={max_rows}",
        env=settings.environment,
        capture="full" if settings.debug_trace_active else "none",
        trace="on" if settings.debug_trace_active else "off",
        level=settings.effective_log_level,
        retention=settings.debug_trace_retention_hours,
        max_bytes=settings.debug_trace_max_payload_bytes,
        max_rows=settings.debug_trace_max_rows,
    )


def configure_logging(settings: FactoryAgentSettings) -> None:
    """Install sinks and intercept standard-library logging once at startup."""
    from loguru import logger as loguru_logger

    loguru_logger.remove()
    # ``effective_log_level`` withholds DEBUG outside developer environments
    # (decision B, ADR-0004 §Log Levels): debug records carry far more of a
    # payload than INFO ones, so honouring the request on a deployment that may
    # not hold content would reopen the leak the environment tier closes.
    level = settings.effective_log_level
    if settings.log_format == "json":
        loguru_logger.add(_build_json_sink(settings.environment), level=level, enqueue=False)
    else:
        loguru_logger.add(_console_sink, level=level, enqueue=False)

    third_party_level = _coerce_level(settings.log_third_party_level)
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [_InterceptHandler()]
        std_logger.propagate = False
        # ``NOTSET`` preserves the previous behaviour for the libraries whose
        # INFO records are real events (startup, DDL, connection pools).
        std_logger.setLevel(third_party_level if name in _NOISY_LOGGERS else logging.NOTSET)

    _log_effective_environment(settings)


def get_logger(component: str) -> Any:
    """Return a bound logger facade; keeps Loguru out of application modules."""
    from loguru import logger as loguru_logger

    bound: LoguruLogger[Any] = loguru_logger.bind(component=component)  # type: ignore[assignment]
    return bound


__all__ = ["configure_logging", "get_logger"]
