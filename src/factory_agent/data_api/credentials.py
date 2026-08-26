"""Customer credential bundle: the trusted identity entry (Story 5, M1/M4/M15).

The bundle mirrors the customer ``/api/system/token`` response. It is a frozen
value object that only ``data_api/`` may read; credential values never enter
LLM prompts, logs, traces, errors, audit events, or test snapshots.

Identity binding rules:
- ``tenant_id`` is the plaintext ``app_key`` (one factory, one AppKey — M4).
- ``employee_id`` / ``user_id`` is the token response ``user`` (work number);
  the display name is ``uname`` (M10).
- ``roles`` / ``permissions`` are parsed but always empty today (M11) and never
  participate in authorization decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from factory_agent.domain.identifiers import TenantId
from factory_agent.domain.identity import EmployeeId, UserId


@dataclass(frozen=True, slots=True)
class MesCredentialBundle:
    """Trusted credential package issued by the customer token endpoint."""

    access_token: str
    app_key: str
    sign: str
    timestamp: int
    expires_at: datetime
    user: UserId
    uname: str
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.access_token or not self.app_key or not self.sign:
            raise ValueError("credential bundle values must be non-empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

    @property
    def tenant_id(self) -> TenantId:
        """AppKey is the tenant ID (M4); no other source may define it."""
        return TenantId(self.app_key)

    @property
    def employee_id(self) -> EmployeeId:
        """Token ``user`` (work number) is the employee ID (M10)."""
        return EmployeeId(self.user)

    def seconds_until_expiry(self, now: datetime) -> int:
        return int((self.expires_at - now).total_seconds())

    def needs_refresh(self, now: datetime, threshold_seconds: int) -> bool:
        """Proactive refresh when inside the threshold (default 90 minutes)."""
        return self.seconds_until_expiry(now) <= threshold_seconds


__all__ = ["MesCredentialBundle"]
