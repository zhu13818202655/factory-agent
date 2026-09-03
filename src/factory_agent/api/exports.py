"""Instant export download endpoint (Story 3: 即时生成、直接下载、不留存).

Download re-validates the caller through the token exchange, resolves the
current authorization, and then streams the transient in-memory XLSX back as a
file response. There is no object store and no presigned URL: content lives in
a short-TTL in-process buffer and is released when the response ends. A
missing, expired, or foreign export id is a plain 404 — regeneration goes
through history/favorite re-ask. A download audit event records only the
artifact ID, tenant, and outcome — never the row detail.
"""

from __future__ import annotations

from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status

from factory_agent.api.identity import resolve_credential
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.observability.audit import AuditEvent, AuditEventType, AuditOutcome
from factory_agent.ports.session import InteractionOwner

export_router = APIRouter(prefix="/v1", tags=["artifacts"])

#: Exports are served as an attachment; browsers download the stream directly
#: and App clients save it to local storage.
_DISPOSITION_ASCII_FALLBACK = "export.xlsx"


def _content_disposition(filename: str) -> str:
    return (
        f'attachment; filename="{_DISPOSITION_ASCII_FALLBACK}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


@export_router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    request: Request,
) -> Response:
    container = _container(request)
    exporter = container.artifact_exporter
    if exporter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="artifact service is not configured",
        )
    credential, _ = await resolve_credential(request)
    try:
        authorization = await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc

    owner = InteractionOwner(
        tenant_id=authorization.tenant_context.tenant_id,
        user_id=authorization.tenant_context.user_id,
    )
    content = await exporter.fetch(owner, artifact_id)
    if content is None:
        # Indistinguishable for foreign/expired/missing ids; regeneration goes
        # through history/favorite re-ask (重新执行 → 直接下载).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="export is no longer available; re-ask from history/favorites to regenerate",
        )

    await container.audit.record(
        AuditEvent(
            event_type=AuditEventType.DOWNLOAD,
            outcome=AuditOutcome.ALLOWED,
            capability_id=None,
            intent_summary=None,
            scope_fingerprint=None,
            employee_count=None,
            dept_count=None,
            whole_tenant=False,
            tenant_id=str(authorization.tenant_context.tenant_id),
            status="allowed",
            occurred_at=container.clock.now(),
            request_id=str(artifact_id),
        )
    )
    return Response(
        content=content.content,
        media_type=content.content_type,
        headers={
            "Content-Disposition": _content_disposition(content.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["export_router"]
