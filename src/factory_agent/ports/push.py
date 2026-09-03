"""Push delivery port (Story 3B).

Real push channels (企业微信/App/短信) are not yet chosen by the customer, so
this story ships the channel port plus a local fake channel (structured log +
delivery record). Delivery records carry only the envelope — never the message
body or business amounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from factory_agent.domain import TenantId, UserId

PushKind = Literal["morning_report", "subscription"]
DeliveryStatus = Literal["delivered", "failed"]


@dataclass(frozen=True, slots=True)
class PushDelivery:
    delivery_id: str
    tenant_id: TenantId
    user_id: UserId
    kind: PushKind
    status: DeliveryStatus
    created_at: datetime
    content_item_id: str | None = None
    message_digest: str | None = None
    row_count: int | None = None


class PushDeliveryStore(Protocol):
    async def record(self, delivery: PushDelivery) -> None: ...


class PushChannel(Protocol):
    """Delivers one rendered message to a recipient.

    Returns True on success. Implementations must never raise into callers:
    failures are recorded (failed delivery) and never block other work.
    """

    async def deliver(
        self,
        *,
        tenant_id: TenantId,
        user_id: UserId,
        kind: PushKind,
        content_item_id: str | None,
        message_digest: str,
        row_count: int,
        now: datetime,
    ) -> bool: ...


__all__ = [
    "DeliveryStatus",
    "PushChannel",
    "PushDelivery",
    "PushDeliveryStore",
    "PushKind",
]
