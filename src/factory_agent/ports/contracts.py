from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, TypeVar

from factory_agent.domain import (
    DeptId,
    InteractionId,
    Role,
    TenantId,
    TenantMembership,
    UserId,
)

MesRequestT = TypeVar("MesRequestT", contravariant=True)
MesResponseT = TypeVar("MesResponseT", covariant=True)

#: Column-level sentinel for a metric with no confirmed data source (see the
#: metric registry's ``unavailable`` entries). Renderers must surface it as an
#: explicit "no data source" state, never as a fabricated number. Lives in
#: ``ports`` so both the execution kernel and the application renderers share
#: the same contract without crossing packages.
UNAVAILABLE_VALUE = "unavailable"


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    subject_id: str


@dataclass(frozen=True, slots=True)
class TrustedCredential:
    """Trusted credential pair that uniquely locates one tenant membership."""

    tenant_id: TenantId
    user_id: UserId


@dataclass(frozen=True, slots=True)
class ResolvedPrincipal:
    """Identity facts exchanged from the customer token endpoint.

    Derived exclusively from the ``/api/system/token`` response: the role code
    ``roles`` (00/01/02/99) and the bound department set are authoritative
    (客户确认：绑定关系登录后由 token 接口返回). No credential value
    (accessToken/sign/app_key) is ever exposed here.
    """

    credential: TrustedCredential
    display_name: str
    role: Role
    bound_dept_ids: tuple[DeptId, ...] = ()


class CredentialExchange(Protocol):
    """Exchanges the caller's encrypted app_key for token-derived identity.

    Implementations live in ``data_api/``: they call ``/api/system/token``,
    retain the credential bundle internally, and expose only identity facts.
    A rejected exchange raises ``UnauthenticatedError``.
    """

    async def authenticate(self, encrypted_credential: str) -> ResolvedPrincipal: ...

    def principal_for(self, credential: TrustedCredential) -> ResolvedPrincipal | None:
        """Return the cached principal behind an already-authenticated pair."""
        ...


class CredentialBinder(Protocol):
    """Binds the live credential bundle to the current interaction context.

    MES calls executed inside the returned context use that caller's bundle;
    outside of any binding the adapter falls back to its injected default.
    """

    def bind_for(self, credential: TrustedCredential) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class SessionRecord:
    interaction_id: InteractionId
    tenant_id: TenantId
    payload: bytes


class IdentityProvider(Protocol):
    async def authenticate(self, credential: str) -> AuthenticatedIdentity: ...


class MembershipResolver(Protocol):
    """Resolve the unique membership for a trusted credential pair.

    The binding comes from the customer credential bundle (tenant =
    plaintext app_key, employee = token ``user``); one factory has one AppKey,
    so membership is naturally unique. Implementations raise
    ``MembershipNotFoundError`` when no active employee record exists.
    """

    async def resolve(
        self,
        credential: TrustedCredential,
        as_of: datetime,
    ) -> TenantMembership: ...


@dataclass(frozen=True, slots=True)
class ResourceFetchResult:
    """One provably-complete (or explicitly incomplete) paged resource fetch.

    ``rows`` holds validated customer rows; ``footer`` carries the optional MES
    ``result.footer`` totals (e.g. ``je_total``) as a string mapping. When
    pagination verification fails (total drift, duplicate/missing page, budget
    exhaustion) ``complete`` is ``False`` and ``reason`` names the anomaly so the
    caller can surface a structured state instead of a fabricated number.
    """

    rows: tuple[dict[str, object], ...]
    total: int
    pages_fetched: int
    complete: bool
    reason: str | None = None
    footer: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RenderColumn:
    """One output column with optional type/unit for card and Excel rendering."""

    name: str
    metric_name: str | None
    metric_version: str | None
    source_operations: tuple[str, ...]
    column_type: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class RenderTable:
    """Vendor-neutral renderable result consumed by card and Excel renderers.

    Carries only the numbers and approved metadata needed to render a structured
    card and an XLSX file; it never carries customer field names, credentials,
    or row filtering internals.
    """

    capability_id: str
    columns: tuple[RenderColumn, ...]
    rows: tuple[dict[str, object], ...]
    totals: dict[str, Decimal]
    source_operations: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    incomplete: bool = False
    incomplete_reason: str | None = None


class MesDataSource(Protocol[MesRequestT, MesResponseT]):
    async def execute(self, request: MesRequestT) -> MesResponseT: ...


class SessionRepository(Protocol):
    async def get(self, interaction_id: InteractionId) -> SessionRecord | None: ...

    async def put(self, record: SessionRecord) -> None: ...


class ArtifactStore(Protocol):
    async def put(self, artifact_id: str, content: bytes, content_type: str) -> None: ...

    async def get(self, artifact_id: str) -> bytes: ...

    async def delete(self, artifact_id: str) -> None: ...

    async def presign(self, artifact_id: str, expires_in_seconds: int) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
