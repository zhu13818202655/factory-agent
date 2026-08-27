"""Internal idempotent usage-event ingest endpoint.

This is the only write path from factory-agent's publisher. It enforces the
batch caps, validates every event against the v1 whitelist, and returns
per-event accepted/duplicate/rejected outcomes so the publisher can dead-letter
permanently rejected events and retry transient ones.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from usage_admin.config import UsageAdminSettings
from usage_admin.container import AdminContainer

ingest_router = APIRouter(prefix="/internal/v1", tags=["internal"])


class UsageEventBatchRequest(BaseModel):
    events: list[dict[str, object]] = Field(default_factory=list[dict[str, object]])


class UsageEventBatchResponse(BaseModel):
    accepted: list[str]
    duplicate: list[str]
    rejected: list[str]
    reasons: dict[str, str] = Field(default_factory=dict)
    batch_reason: str | None = None


def _container(request: Request) -> AdminContainer:
    return cast(AdminContainer, request.app.state.container)


def _require_ingest_auth(request: Request) -> None:
    settings = cast(UsageAdminSettings, request.app.state.settings)
    api_key = settings.ingest_api_key
    if api_key is None:
        return
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {api_key.get_secret_value()}"
    if auth != expected:
        raise HTTPException(status_code=401, detail="invalid ingest credentials")


@ingest_router.post(
    "/usage-events:batch",
    response_model=UsageEventBatchResponse,
    dependencies=[Depends(_require_ingest_auth)],
)
async def ingest_batch(
    request: Request,
    body: UsageEventBatchRequest,
) -> UsageEventBatchResponse:
    ingest = _container(request).ingest
    result = await ingest.ingest(body.events)
    return UsageEventBatchResponse(
        accepted=list(result.accepted),
        duplicate=list(result.duplicate),
        rejected=list(result.rejected),
        reasons=result.reasons,
        batch_reason=result.batch_reason,
    )
