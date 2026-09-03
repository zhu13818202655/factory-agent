"""Customer credential bundle: the trusted identity entry.

The bundle mirrors the customer ``/api/system/token`` response. It is a frozen
value object that only ``data_api/`` may read; credential values never enter
LLM prompts, logs, traces, errors, audit events, or test snapshots.

Identity binding rules (source: ``docs/product/AI问答对外接口-整理.md`` §2.1
and ``docs/product/需求及方案整理.md``「客户确认结论」):
- ``tenant_id`` is the plaintext ``app_key`` (one factory, one AppKey).
- ``employee_id`` / ``user_id`` is the token response ``user`` (work number);
  the display name is ``uname``.
- ``roles`` is the authoritative role code (00 员工 / 01 组长 / 02 管理 /
  99 老板) and ``dept`` the bound department; managers may bind multiple
  departments/workshops (``bound_depts``), returned by the token endpoint at
  login. Both feed ``TenantContext`` and the capability-role matrix.
- The bundle ``timestamp`` has a short validity (default 60 seconds); a stale
  timestamp triggers a token re-exchange before the next business call.
"""

from __future__ import annotations

from contextvars import ContextVar
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
    #: The token-returned home/bound department code (authoritative identity
    #: field; empty only in degraded test fixtures).
    dept: str = ""
    #: Full bound department/workshop set for managers (may span workshops);
    #: falls back to ``(dept,)`` when the token returns a single binding.
    bound_depts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.access_token or not self.app_key or not self.sign:
            raise ValueError("credential bundle values must be non-empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

    @property
    def tenant_id(self) -> TenantId:
        """AppKey is the tenant ID; no other source may define it."""
        return TenantId(self.app_key)

    @property
    def employee_id(self) -> EmployeeId:
        """Token ``user`` (work number) is the employee ID."""
        return EmployeeId(self.user)

    @property
    def effective_bound_depts(self) -> tuple[str, ...]:
        """Bound dept set with the single-``dept`` fallback applied."""
        if self.bound_depts:
            return self.bound_depts
        if self.dept:
            return (self.dept,)
        return ()

    def seconds_until_expiry(self, now: datetime) -> int:
        return int((self.expires_at - now).total_seconds())

    def needs_refresh(self, now: datetime, threshold_seconds: int) -> bool:
        """Proactive refresh when inside the threshold (default 90 minutes)."""
        return self.seconds_until_expiry(now) <= threshold_seconds

    def timestamp_age_seconds(self, now: datetime) -> int:
        """Age of the token-issued ``timestamp`` in seconds."""
        return int(now.timestamp()) - self.timestamp

    def timestamp_is_stale(self, now: datetime, ttl_seconds: int) -> bool:
        """The customer ``timestamp`` is only valid for a short window.

        Refresh is triggered a few seconds before the window closes so clock
        skew between the agent and the MES never produces a spurious
        ``请求已过期`` failure (default window 60 s, 客户接口文档 §2.1).
        A zero timestamp marks a placeholder bundle and is never stale.
        """
        if self.timestamp <= 0:
            return False
        margin = min(5, max(ttl_seconds // 4, 1))
        return self.timestamp_age_seconds(now) >= ttl_seconds - margin


#: Live bundle for the interaction currently executing MES calls. Set by the
#: token gateway's per-caller binding; the adapter reads it at send time and
#: falls back to its injected default bundle outside any binding.
CURRENT_BUNDLE: ContextVar[MesCredentialBundle | None] = ContextVar(
    "factory_agent_mes_credential_bundle", default=None
)


__all__ = ["CURRENT_BUNDLE", "MesCredentialBundle"]
