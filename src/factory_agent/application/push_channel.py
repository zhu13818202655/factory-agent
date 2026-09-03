"""Local fake push channel (Story 3B).

Real channels (企业微信/App/短信) await a customer decision; until then every
delivery goes through this port implementation which writes a structured log
and an envelope-only delivery record. A failure never blocks the caller.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from factory_agent.domain import TenantId, UserId
from factory_agent.observability.logging_adapter import get_logger
from factory_agent.ports.push import (
    PushDelivery,
    PushDeliveryStore,
    PushKind,
)

_logger = get_logger("push.channel")


class IdFactory(Protocol):
    def __call__(self) -> str: ...


class LocalPushChannel:
    """Structured-log + record delivery; no third-party transport yet."""

    def __init__(
        self,
        store: PushDeliveryStore | None = None,
        *,
        new_id: IdFactory | None = None,
    ) -> None:
        self._store = store
        self._new_id = new_id or (lambda: uuid4_hex())

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
    ) -> bool:
        delivery = PushDelivery(
            delivery_id=self._new_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            kind=kind,
            status="delivered",
            created_at=now,
            content_item_id=content_item_id,
            message_digest=message_digest,
            row_count=row_count,
        )
        try:
            if self._store is not None:
                await self._store.record(delivery)
        except Exception:  # noqa: BLE001 - a failed delivery must never raise
            _logger.opt(exception=True).warning(
                "push.channel.record_failed",
            )
            return False
        _logger.bind(
            kind=kind,
            tenant_id=str(tenant_id),
            content_item_id=content_item_id,
            row_count=row_count,
        ).info("push.channel.delivered")
        return True


def uuid4_hex() -> str:
    from uuid import uuid4

    return uuid4().hex


__all__ = ["LocalPushChannel"]
