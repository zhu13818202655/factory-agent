"""Loguru-backed logging adapter; application code never imports Loguru."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

from factory_agent.config import FactoryAgentSettings
from factory_agent.observability.context import current_log_context
from factory_agent.observability.redaction import redact_mapping, redact_text

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


class _InterceptHandler(logging.Handler):
    """Forward standard logging records into the Loguru sink."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue code
        try:
            from loguru import logger as loguru_logger

            level: str | int
            level = record.levelname
            loguru_logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())
        except Exception:
            logging.getLogger(__name__).debug("log forwarding dropped a record")


def _json_sink(message: Any) -> None:  # pragma: no cover - exercised via logging capture
    record = message.record
    from factory_agent import __version__

    payload = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "service": "factory-agent",
        "version": __version__,
        "component": record["extra"].get("component", "app"),
        "event": record["message"],
        **current_log_context(),
        **redact_mapping(
            {key: value for key, value in record["extra"].items() if key != "component"}
        ),
    }
    if record["exception"] is not None:
        payload["error_type"] = type(record["exception"]).__name__
    sys.stdout.write(repr(payload) + "\n")


def _console_sink(message: Any) -> None:  # pragma: no cover - local development only
    import traceback as _traceback

    record = message.record
    context = current_log_context()
    request_id = context.get("request_id", "-")
    sys.stdout.write(
        f"{record['time'].isoformat()} | {record['level'].name:<8} | "
        f"{request_id} | {record['extra'].get('component', 'app')} | "
        f"{redact_text(record['message'])}\n"
    )
    # Attach the real traceback so local debugging never hides the root cause
    # behind a structured marker (e.g. ``interaction.execution_failed``).
    if record["exception"] is not None:
        rendered = "".join(_traceback.format_exception(*record["exception"]))
        sys.stdout.write(redact_text(rendered) + "\n")


def configure_logging(settings: FactoryAgentSettings) -> None:
    """Install sinks and intercept standard-library logging once at startup."""
    from loguru import logger as loguru_logger

    loguru_logger.remove()
    serialize = settings.log_format == "json"
    if serialize:
        loguru_logger.add(_json_sink, level=settings.log_level, enqueue=False)
    else:
        loguru_logger.add(_console_sink, level=settings.log_level, enqueue=False)

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [_InterceptHandler()]
        std_logger.propagate = False


def get_logger(component: str) -> Any:
    """Return a bound logger facade; keeps Loguru out of application modules."""
    from loguru import logger as loguru_logger

    bound: LoguruLogger[Any] = loguru_logger.bind(component=component)  # type: ignore[assignment]
    return bound


__all__ = ["configure_logging", "get_logger"]
