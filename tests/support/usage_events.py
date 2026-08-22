from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, Mapping

UsageEvent = Mapping[str, object]
IngestStatus = Literal["accepted", "duplicate", "rejected"]


@dataclass
class FakeUsageEventProducer:
    events: list[dict[str, object]] = field(default_factory=lambda: [])

    def emit(self, event: UsageEvent) -> None:
        self.events.append(deepcopy(dict(event)))


@dataclass
class FakeUsageEventIngest:
    digests: dict[str, str] = field(default_factory=lambda: {})
    events: list[dict[str, object]] = field(default_factory=lambda: [])

    def ingest(self, event: UsageEvent) -> IngestStatus:
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            return "rejected"
        digest = hashlib.sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        previous = self.digests.get(event_id)
        if previous is not None:
            return "duplicate" if previous == digest else "rejected"
        self.digests[event_id] = digest
        self.events.append(deepcopy(dict(event)))
        return "accepted"


@dataclass
class FakeUsageEventRollup:
    counts: Counter[str] = field(default_factory=lambda: Counter[str]())

    def apply(self, events: list[dict[str, object]]) -> None:
        for event in events:
            event_type = event.get("event_type")
            if isinstance(event_type, str):
                self.counts[event_type] += 1
