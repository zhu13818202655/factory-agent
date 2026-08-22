"""Protocols implemented by external adapters."""

from factory_agent.ports.cache import CacheStore
from factory_agent.ports.contracts import (
    ArtifactStore,
    AuthenticatedIdentity,
    Clock,
    IdentityProvider,
    MesDataSource,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    SessionRecord,
    SessionRepository,
)

__all__ = [
    "ArtifactStore",
    "CacheStore",
    "AuthenticatedIdentity",
    "Clock",
    "IdentityProvider",
    "MesDataSource",
    "ModelGateway",
    "ModelRequest",
    "ModelResponse",
    "SessionRecord",
    "SessionRepository",
]
