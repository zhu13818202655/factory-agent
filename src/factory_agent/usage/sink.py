"""Batch HTTP transport to usage-admin.

The sink is deliberately dumb: it posts a batch, reports which ``event_id``
values were accepted, and lets the publisher own retry and dead-letter policy.
Duplicate ``event_id`` values are safe because usage-admin ingests idempotently.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self, cast

import httpx

from factory_agent.ports import OutboxRecord


class UsagePublishError(RuntimeError):
    """The batch was not accepted; the publisher decides whether to retry."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class HttpUsageEventSink:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def publish(self, records: tuple[OutboxRecord, ...]) -> tuple[str, ...]:
        if not records:
            return ()
        payload: dict[str, Any] = {"events": [record.payload for record in records]}
        try:
            response = await self._client.post("/internal/usage-events", json=payload)
        except httpx.TimeoutException as exc:
            raise UsagePublishError("usage-admin timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise UsagePublishError("usage-admin is unreachable", retryable=True) from exc

        if response.status_code in (408, 429) or response.status_code >= 500:
            raise UsagePublishError(
                f"usage-admin returned HTTP {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise UsagePublishError(
                f"usage-admin rejected the batch with HTTP {response.status_code}",
                retryable=False,
            )

        try:
            body: object = response.json()
        except ValueError:
            body = None
        accepted = _accepted_ids(body)
        return accepted if accepted else tuple(record.event_id for record in records)


def _accepted_ids(body: object) -> tuple[str, ...]:
    if not isinstance(body, dict):
        return ()
    raw: object = cast("dict[str, object]", body).get("accepted")
    if not isinstance(raw, list):
        return ()
    items = cast("list[object]", raw)
    return tuple(item for item in items if isinstance(item, str))


__all__ = ["HttpUsageEventSink", "UsagePublishError"]
