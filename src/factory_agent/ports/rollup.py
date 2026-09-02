"""Rollup row types shared by the application engine and the persistence store.

Defined in ``ports`` so the application layer (which may depend only on
``domain`` and ``ports``) can drive the rollup computation without importing
``persistence`` (Story 11 3; package-boundary test in
``tests/security/test_package_boundaries.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class InteractionFactRow:
    tenant_id: str
    occurred_at: datetime
    event_type: str
    user_subject_id: str
    capability_id: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    mes_duration_ms: int | None = None
    llm_duration_ms: int | None = None
    local_duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class LlmCallFactRow:
    tenant_id: str
    occurred_at: datetime
    logical_call_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class MesCallFactRow:
    tenant_id: str
    occurred_at: datetime
    operation_id: str
    status: str


@dataclass(frozen=True, slots=True)
class RollupRow:
    tenant_id: str
    bucket_start: datetime
    metric: str
    value: float
    rollup_version: str
    rolled_up_at: datetime
    granularity: str = "hour"


__all__ = [
    "InteractionFactRow",
    "LlmCallFactRow",
    "MesCallFactRow",
    "RollupRow",
]
