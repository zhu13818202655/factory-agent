"""Loguru-backed logging facade for usage-admin (mirrors factory-agent's adapter).

Application modules bind a component logger through ``get_logger`` so every
record carries the owning component. Standard-library ``logging`` from third
parties is left untouched: usage-admin owns no heavy runtime libraries beyond
its own API, so no interception bridge is installed here.

``configure_logging`` installs a single stderr sink; call it once at process
startup (the API ``create_app`` entrypoint already does).
"""

from __future__ import annotations

import sys
from typing import Any

from loguru import logger as _loguru_logger


def get_logger(component: str) -> Any:
    """Return a bound Loguru logger for one component."""
    return _loguru_logger.bind(component=component)


def configure_logging(level: str = "INFO") -> None:
    """Install a single stderr sink; safe to call repeatedly."""
    _loguru_logger.remove()
    _loguru_logger.add(sys.stderr, level=level)


__all__ = ["configure_logging", "get_logger"]
