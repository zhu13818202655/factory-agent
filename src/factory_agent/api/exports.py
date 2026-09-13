"""Instant export download endpoint (即时生成、直接下载、不留存).

Download re-validates the caller through the token exchange, resolves the
current authorization, and then streams the transient in-memory XLSX back as a
file response. There is no object store and no presigned URL: content lives in
a short-TTL in-process buffer and is released when the response ends. A
missing, expired, or foreign export id is a plain 404 — regeneration goes
through history/favorite re-ask. A download audit event records the artifact
ID, tenant, outcome, and an irreversible scope digest — never the row detail.
"""

from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status

from factory_agent.api.identity import resolve_credential
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditWriteError,
    scope_fingerprint,
)
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

    scope = authorization.data_scope
    try:
        await container.audit.record(
            AuditEvent(
                event_type=AuditEventType.DOWNLOAD,
                outcome=AuditOutcome.ALLOWED,
                capability_id=None,
                intent_summary=None,
                scope_fingerprint=scope_fingerprint(
                    str(authorization.tenant_context.tenant_id),
                    tuple(scope.employee_ids),
                    tuple(scope.dept_ids),
                ),
                employee_count=len(scope.employee_ids),
                dept_count=len(scope.dept_ids),
                whole_tenant=scope.mes_filtered,
                tenant_id=str(authorization.tenant_context.tenant_id),
                status="allowed",
                occurred_at=container.clock.now(),
                request_id=str(artifact_id),
            )
        )
    except AuditWriteError as exc:
        # Releasing payroll data without an audit record is not acceptable
        # (DEC-014): the bytes stay in the transient buffer, and the caller can
        # retry once the sink recovers.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit is unavailable; the export was not released",
        ) from exc
    return Response(
        content=content.content,
        media_type=content.content_type,
        headers={
            "Content-Disposition": _content_disposition(content.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["export_router"]
