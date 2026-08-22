from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from factory_agent.domain import InteractionId, TenantId


@pytest.mark.parametrize("identifier_type", [TenantId, InteractionId])
@pytest.mark.parametrize("value", ["", " tenant-1", "tenant-1 "])
def test_identifier_rejects_empty_or_untrimmed_values(
    identifier_type: type[TenantId] | type[InteractionId], value: str
) -> None:
    with pytest.raises(ValueError):
        identifier_type(value)


def test_identifiers_are_immutable_and_render_as_their_value() -> None:
    tenant_id = TenantId("tenant-1")

    assert str(tenant_id) == "tenant-1"
    with pytest.raises(FrozenInstanceError):
        tenant_id.value = "tenant-2"  # type: ignore[misc]
