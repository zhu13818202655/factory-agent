from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from factory_agent.domain import InteractionId, TenantId

MesRequestT = TypeVar("MesRequestT", contravariant=True)
MesResponseT = TypeVar("MesResponseT", covariant=True)


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    subject_id: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model_alias: str
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    actual_model: str


@dataclass(frozen=True, slots=True)
class SessionRecord:
    interaction_id: InteractionId
    tenant_id: TenantId
    payload: bytes


class IdentityProvider(Protocol):
    async def authenticate(self, credential: str) -> AuthenticatedIdentity: ...


class MesDataSource(Protocol[MesRequestT, MesResponseT]):
    async def execute(self, request: MesRequestT) -> MesResponseT: ...


class ModelGateway(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


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
