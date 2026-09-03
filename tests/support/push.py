"""In-memory push fakes for tests (Story 3B)."""

from __future__ import annotations

from dataclasses import dataclass, field

from factory_agent.domain import TenantId, UserId
from factory_agent.ports.push import PushDelivery
from factory_agent.ports.push_preferences import PushPreferences


@dataclass
class InMemoryPushPreferenceRepository:
    records: dict[tuple[str, str], PushPreferences] = field(default_factory=lambda: {})

    async def get(self, tenant_id: TenantId, user_id: UserId) -> PushPreferences | None:
        return self.records.get((str(tenant_id), str(user_id)))

    async def upsert(self, prefs: PushPreferences) -> None:
        self.records[(str(prefs.tenant_id), str(prefs.user_id))] = prefs


@dataclass
class InMemoryPushDeliveryStore:
    deliveries: list[PushDelivery] = field(default_factory=lambda: [])

    async def record(self, delivery: PushDelivery) -> None:
        self.deliveries.append(delivery)
