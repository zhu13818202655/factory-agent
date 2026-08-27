"""Alert sink boundary for ingest conflicts and rollup anomalies.

Alerts carry metadata only — never event payloads, prompts, or identities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

_LOGGER = logging.getLogger("usage_admin.alerts")


@dataclass(frozen=True, slots=True)
class AlertRecord:
    kind: str
    detail: dict[str, object]
    created_at: str


class AlertSink(Protocol):
    async def alert(self, kind: str, detail: dict[str, object]) -> None: ...


@dataclass
class LoggingAlertSink:
    """Writes structured metadata-only alerts to the application log."""

    records: list[AlertRecord] = field(default_factory=list[AlertRecord])
    _capture: bool = True

    async def alert(self, kind: str, detail: dict[str, object]) -> None:
        if self._capture:
            self.records.append(AlertRecord(kind=kind, detail=dict(detail), created_at=str(detail)))
        _LOGGER.warning("usage.alert %s %s", kind, detail)


@dataclass
class CollectingAlertSink:
    """In-memory alert sink for tests and offline runs."""

    records: list[AlertRecord] = field(default_factory=list[AlertRecord])

    async def alert(self, kind: str, detail: dict[str, object]) -> None:
        self.records.append(AlertRecord(kind=kind, detail=dict(detail), created_at=""))


__all__ = ["AlertRecord", "AlertSink", "CollectingAlertSink", "LoggingAlertSink"]
