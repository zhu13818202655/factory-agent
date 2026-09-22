"""Trace endpoints (plan §4.5).

Three read-only routes, two of which exist in every environment:

===============================  ==========================  ===============
Route                            Mounted when                Content
===============================  ==========================  ===============
``/v1/trace/capabilities``       always                      capability flags
``/v1/interactions/{id}/trace``  always                      A-level facts
``/v1/interactions/{id}/trace.html``  ``local`` / ``dev`` **and** capture on  content
===============================  ==========================  ===============

The third route is *not registered* outside a developer environment rather than
registered-then-refused. Two consequences follow, and both are intended: the
production surface has one fewer attack vector, and a caller cannot tell a
misconfigured deployment from a missing one — the answer is a plain 404 either
way, exactly as the plan requires ("路由不注册（404，非 403）").

Access control adds no new credential. It reuses the four-segment shape already
proved by ``api/exports.py``: resolve the credential, authorize it, fetch inside
the ``(tenant_id, user_id)`` ownership boundary, and only release bytes once the
audit record has landed. An audit outage therefore withholds the report (503)
instead of releasing it silently (DEC-014).
"""

from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from factory_agent.api.identity import require_tenant_enabled, resolve_credential
from factory_agent.application.authorization import IdentityRejectionError, ResolvedAuthorization
from factory_agent.application.trace import TraceService
from factory_agent.application.trace_report import render_trace_report
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import InteractionId
from factory_agent.observability.audit import (
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditWriteError,
    scope_fingerprint,
)
from factory_agent.observability.context import current_request_id
from factory_agent.ports.session import InteractionOwner

#: Capability discovery and the A-level timeline: mounted in every environment.
trace_router = APIRouter(
    prefix="/v1", tags=["trace"], dependencies=[Depends(require_tenant_enabled)]
)

#: The content-bearing report. Included only for a developer environment with
#: capture switched on; see this module's docstring for why it is absent rather
#: than guarded.
trace_report_router = APIRouter(
    prefix="/v1", tags=["trace"], dependencies=[Depends(require_tenant_enabled)]
)

_REPORT_ASCII_FALLBACK = "trace.html"

#: One message for "not yours" and "does not exist". The report route adds a
#: third case — "this environment has no report route" — which the router
#: answers before this code runs, so it too is indistinguishable from the
#: caller's side. The front end must not narrate a specific cause from a 404.
_INTERACTION_GONE_DETAIL = "interaction not found"


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


def _trace_service(request: Request) -> TraceService:
    service = _container(request).trace
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="trace service is not configured",
        )
    return service


async def _resolve_owner(
    request: Request,
) -> tuple[ApplicationContainer, ResolvedAuthorization, InteractionOwner]:
    """The first three segments of the shared download shape."""
    container = _container(request)
    credential, _ = await resolve_credential(request)
    try:
        authorization = await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc
    owner = InteractionOwner(
        tenant_id=authorization.tenant_context.tenant_id,
        user_id=authorization.tenant_context.user_id,
    )
    return container, authorization, owner


@trace_router.get("/trace/capabilities")
async def trace_capabilities(request: Request) -> dict[str, Any]:
    """Answer with the deployment's capture ceiling.

    A ``200`` in every environment, including production, where
    ``report_available`` is ``false``. The front end decides whether to render
    the download entry point from the field, never from the status code: a
    status-coded answer would make "unsupported" and "broken" the same signal.
    """
    capability = _container(request).trace_capability
    return {
        "environment": capability.environment,
        "report_available": capability.report_available,
        "content_capture": capability.content_capture,
        "reason": capability.reason,
    }


@trace_router.get("/interactions/{interaction_id}/trace")
async def read_trace(interaction_id: str, request: Request) -> dict[str, Any]:
    service = _trace_service(request)
    _, _, owner = await _resolve_owner(request)
    document = await service.build(
        owner, InteractionId(interaction_id), request_id=current_request_id()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_INTERACTION_GONE_DETAIL)
    return document


@trace_report_router.get("/interactions/{interaction_id}/trace.html")
async def download_trace_report(interaction_id: str, request: Request) -> Response:
    """Render and release the self-contained report.

    Mounted only where capture is possible, so an unreachable store here means
    the deployment is misconfigured rather than that the feature is off.
    """
    service = _trace_service(request)
    if not service.capability.report_available:
        # Defensive: the router should not be mounted at all in this state, so
        # reaching here means the mount decision and the capability disagree.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_INTERACTION_GONE_DETAIL)
    container, authorization, owner = await _resolve_owner(request)
    document = await service.build(
        owner, InteractionId(interaction_id), request_id=current_request_id()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_INTERACTION_GONE_DETAIL)
    html = render_trace_report(document)
    await _record_download(container, authorization, interaction_id)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": _report_content_disposition(
                f"链路追踪报告-{interaction_id}.html"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _record_download(
    container: ApplicationContainer,
    authorization: ResolvedAuthorization,
    interaction_id: str,
) -> None:
    """Audit before releasing the bytes, as the export path does."""
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
                request_id=interaction_id,
            )
        )
    except AuditWriteError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit is unavailable; the trace report was not released",
        ) from exc


def _report_content_disposition(filename: str) -> str:
    """Attachment with an ASCII fallback and an RFC 5987 UTF-8 name.

    Both are supplied because the two consumers differ: an App client reads the
    plain ``filename``, a browser prefers ``filename*`` and renders the Chinese
    name. The client-side parser prefers ``filename*`` for the same reason.
    """
    return f"attachment; filename=\"{_REPORT_ASCII_FALLBACK}\"; filename*=UTF-8''{quote(filename)}"


__all__ = ["trace_report_router", "trace_router"]
