"""Export artifact download endpoint.

Download re-validates the trusted tenant (app_key) and user identity header,
resolves the current authorization, and then issues a short-lived presigned URL
from the artifact store. Cross-tenant object keys are unreachable because the
artifact repository is queried by the trusted ownership pair. A download audit
event records only the artifact ID, tenant, and outcome — never the row detail.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from factory_agent.api.sessions import TENANT_HEADER, USER_HEADER
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import TenantId, UserId
from factory_agent.observability.audit import AuditEvent, AuditEventType, AuditOutcome
from factory_agent.ports import TrustedCredential
from factory_agent.ports.artifacts import ExportError
from factory_agent.ports.session import InteractionOwner


class DownloadView(BaseModel):
    artifact_id: str
    url: str
    expires_in_seconds: int


export_router = APIRouter(prefix="/v1", tags=["artifacts"])


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


def _credential(tenant_id: str | None, user_id: str | None) -> TrustedCredential:
    if not tenant_id or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted identity headers are missing"
        )
    try:
        return TrustedCredential(tenant_id=TenantId(tenant_id), user_id=UserId(user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted identity headers are invalid"
        ) from exc


@export_router.get("/artifacts/{artifact_id}/download", response_model=DownloadView)
async def download_artifact(
    artifact_id: str,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
    x_factory_user_id: str | None = Header(default=None, alias=USER_HEADER),
) -> DownloadView:
    container = _container(request)
    exporter = container.artifact_exporter
    if exporter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="artifact service is not configured",
        )
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    try:
        authorization = await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc

    owner = InteractionOwner(
        tenant_id=authorization.tenant_context.tenant_id,
        user_id=authorization.tenant_context.user_id,
    )
    try:
        url = await exporter.presign(owner, artifact_id)
    except ExportError as exc:
        if exc.code == "artifact_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found"
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="artifact download failed"
        ) from exc

    expires_in_seconds = container.settings.artifact_presign_expires_seconds
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
    return DownloadView(artifact_id=artifact_id, url=url, expires_in_seconds=expires_in_seconds)


__all__ = ["export_router"]
