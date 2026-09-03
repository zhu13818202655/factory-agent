"""A deterministic, offline credential exchange for API tests.

``TokenCredentialExchange`` normally calls the MES ``/api/system/token``
endpoint. This subclass never touches the network: ``authenticate`` maps a
presented encrypted credential onto a pre-registered ``ResolvedPrincipal`` and
records the live entry so ``principal_for``/``bind_for`` behave exactly like a
real exchange. It exists only in ``tests/support`` and must never be imported
from production code.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.token_gateway import (
    LiveEntry,
    TokenCredentialExchange,
    digest_of,
)
from factory_agent.domain import DeptId, Role, TenantId, UserId
from factory_agent.domain.errors import UnauthenticatedError
from factory_agent.ports import TrustedCredential
from factory_agent.ports.contracts import ResolvedPrincipal

_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


class FakeCredentialExchange(TokenCredentialExchange):
    """Offline credential exchange keyed by the presented encrypted credential."""

    def __init__(self, principals: Mapping[str, ResolvedPrincipal]) -> None:
        super().__init__("http://token.invalid")
        self._principals: dict[str, ResolvedPrincipal] = dict(principals)
        self.authenticate_calls: list[str] = []

    async def authenticate(self, encrypted_credential: str) -> ResolvedPrincipal:
        self.authenticate_calls.append(encrypted_credential)
        principal = self._principals.get(encrypted_credential)
        if principal is None:
            raise UnauthenticatedError("unknown credential")
        self._register(encrypted_credential, principal)
        return principal

    def _register(self, encrypted: str, principal: ResolvedPrincipal) -> None:
        credential = principal.credential
        bundle = MesCredentialBundle(
            access_token="fake-access-token",
            app_key=str(credential.tenant_id),
            sign="fake-sign",
            timestamp=0,
            expires_at=datetime.max.replace(tzinfo=timezone.utc),
            user=credential.user_id,
            uname=principal.display_name,
            roles=(principal.role.mes_code,),
            bound_depts=tuple(str(dept) for dept in principal.bound_dept_ids),
        )
        entry = LiveEntry(
            principal=principal,
            bundle=bundle,
            encrypted_credential=encrypted,
            exchanged_at=_NOW,
        )
        self._entries_by_digest[digest_of(encrypted)] = entry
        self._entries_by_identity[(str(credential.tenant_id), str(credential.user_id))] = entry


def principal(
    *,
    tenant_id: str,
    user_id: str,
    display_name: str = "模拟员工",
    role: Role = Role.EMPLOYEE,
    bound_depts: tuple[str, ...] = (),
) -> ResolvedPrincipal:
    """Build a ``ResolvedPrincipal`` for the fake exchange."""
    return ResolvedPrincipal(
        credential=TrustedCredential(tenant_id=TenantId(tenant_id), user_id=UserId(user_id)),
        display_name=display_name,
        role=role,
        bound_dept_ids=tuple(DeptId(dept) for dept in bound_depts),
    )


__all__ = ["FakeCredentialExchange", "principal"]
