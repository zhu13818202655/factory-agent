from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from factory_agent.domain import InteractionId
from factory_agent.ports import (
    AuthenticatedIdentity,
    ModelRequest,
    ModelResponse,
    SessionRecord,
)


@dataclass
class FakeIdentityProvider:
    identity: AuthenticatedIdentity
    credentials: list[str] = field(default_factory=lambda: [])

    async def authenticate(self, credential: str) -> AuthenticatedIdentity:
        self.credentials.append(credential)
        return self.identity


@dataclass
class FakeMesDataSource:
    response: object
    requests: list[object] = field(default_factory=lambda: [])

    async def execute(self, request: object) -> object:
        self.requests.append(request)
        return self.response


@dataclass
class FakeModelGateway:
    response: ModelResponse
    requests: list[ModelRequest] = field(default_factory=lambda: [])

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.response


@dataclass
class FakeSessionRepository:
    records: dict[InteractionId, SessionRecord] = field(default_factory=lambda: {})

    async def get(self, interaction_id: InteractionId) -> SessionRecord | None:
        record = self.records.get(interaction_id)
        return deepcopy(record)

    async def put(self, record: SessionRecord) -> None:
        self.records[record.interaction_id] = deepcopy(record)


@dataclass
class FakeArtifactStore:
    objects: dict[str, tuple[bytes, str]] = field(default_factory=lambda: {})

    async def put(self, artifact_id: str, content: bytes, content_type: str) -> None:
        self.objects[artifact_id] = (bytes(content), content_type)

    async def get(self, artifact_id: str) -> bytes:
        return self.objects[artifact_id][0]

    async def delete(self, artifact_id: str) -> None:
        self.objects.pop(artifact_id, None)


@dataclass(frozen=True)
class FakeClock:
    current: datetime = datetime(2026, 8, 21, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current
