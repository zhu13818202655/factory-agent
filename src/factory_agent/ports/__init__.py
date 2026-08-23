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
    TrustedCredential,
)

__all__ = [
    "ArtifactStore",
    "AuthenticatedIdentity",
    "CacheStore",
    "Clock",
    "IdentityProvider",
    "MesDataSource",
    "ModelGateway",
    "ModelRequest",
    "ModelResponse",
    "SessionRecord",
    "SessionRepository",
    "TrustedCredential",
]
