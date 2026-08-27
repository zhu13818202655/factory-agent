from __future__ import annotations

from datetime import datetime
from typing import NoReturn, override

from factory_agent.domain import InteractionId, TenantMembership
from factory_agent.ports.cache import CacheStore
from factory_agent.ports.contracts import (
    ArtifactStore,
    AuthenticatedIdentity,
    Clock,
    IdentityProvider,
    MesDataSource,
    SessionRecord,
    SessionRepository,
    TrustedCredential,
)
from factory_agent.ports.model import (
    ModelErrorCategory,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
)


class DependencyNotConfiguredError(RuntimeError):
    pass


class MembershipNotFoundError(LookupError):
    """No active employee record exists for the trusted credential."""


def _raise(dependency: str) -> NoReturn:
    raise DependencyNotConfiguredError(f"{dependency} is not configured")


class NotConfiguredIdentityProvider(IdentityProvider):
    @override
    async def authenticate(self, credential: str) -> AuthenticatedIdentity:
        _raise("identity provider")


class NotConfiguredMembershipResolver:
    async def resolve(self, credential: TrustedCredential, as_of: datetime) -> TenantMembership:
        _raise("membership resolver")


class NotConfiguredMesDataSource(MesDataSource[object, object]):
    @override
    async def execute(self, request: object) -> object:
        _raise("MES data source")


class NotConfiguredModelGateway(ModelGateway):
    @override
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelGatewayError(
            ModelErrorCategory.NOT_CONFIGURED, "model gateway is not configured"
        )


class NotConfiguredSessionRepository(SessionRepository):
    @override
    async def get(self, interaction_id: InteractionId) -> SessionRecord | None:
        _raise("session repository")

    @override
    async def put(self, record: SessionRecord) -> None:
        _raise("session repository")


class NotConfiguredArtifactStore(ArtifactStore):
    @override
    async def put(self, artifact_id: str, content: bytes, content_type: str) -> None:
        _raise("artifact store")

    @override
    async def get(self, artifact_id: str) -> bytes:
        _raise("artifact store")

    @override
    async def delete(self, artifact_id: str) -> None:
        _raise("artifact store")

    @override
    async def presign(self, artifact_id: str, expires_in_seconds: int) -> str:
        _raise("artifact store")


class NotConfiguredClock(Clock):
    @override
    def now(self) -> datetime:
        _raise("clock")


class NotConfiguredCacheStore(CacheStore):
    @override
    async def get(self, key: str) -> bytes | None:
        _raise("cache store")

    @override
    async def put(self, key: str, value: bytes, ttl_seconds: int) -> None:
        _raise("cache store")

    @override
    async def delete(self, key: str) -> None:
        _raise("cache store")

    @override
    async def delete_prefix(self, prefix: str) -> None:
        _raise("cache store")
