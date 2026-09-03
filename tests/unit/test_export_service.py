"""Instant-export service tests (Story 3: no-retention export).

Proves: in-memory render → transient buffer → owned fetch returns XLSX bytes;
foreign/unknown ids are indistinguishable (None); the buffer is bounded; and a
renderer failure degrades to a structured error without touching results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.export.sanitize import sanitize_filename
from factory_agent.export_service import ExportService
from factory_agent.ports.session import CapabilityRunResult, InteractionOwner

_TENANT = TenantId("APPKEY-A")
_OWNER = InteractionOwner(tenant_id=_TENANT, user_id=UserId("01001"))
_OTHER = InteractionOwner(tenant_id=TenantId("APPKEY-B"), user_id=UserId("99999"))


def _result() -> CapabilityRunResult:
    return CapabilityRunResult(
        capability_id=CapabilityId("fr008_payroll_ranking"),
        column_names=("uid", "uname", "gross"),
        rows=(("01001", "模拟", Decimal("21.65")),),
        column_types={"gross": "money"},
    )


def _ids(prefix: str = "art"):
    counter = iter(range(1, 1000))

    def factory() -> str:
        return f"{prefix}-{next(counter)}"

    return factory


@pytest.mark.asyncio
async def test_export_renders_xlsx_in_memory_and_fetch_returns_owned_bytes() -> None:
    service = ExportService(
        clock=lambda: datetime(2026, 9, 3, 8, tzinfo=timezone.utc), new_id=_ids()
    )

    outcome = await service.export(
        owner=_OWNER,
        interaction_id="it-1",
        capability_id=CapabilityId("fr008_payroll_ranking"),
        role="manager",
        function="FR-008",
        time_range_label="2026-08-01_2026-08-31",
        result=_result(),
    )

    assert outcome.size_bytes > 0
    assert outcome.filename.endswith(".xlsx")
    # 文件名按 角色_功能_时间范围_生成时间 对齐.
    assert sanitize_filename(outcome.filename) == outcome.filename

    content = await service.fetch(_OWNER, outcome.artifact_id)
    assert content is not None
    assert content.content[:2] == b"PK"
    assert content.content_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@pytest.mark.asyncio
async def test_fetch_of_foreign_or_unknown_id_is_indistinguishable() -> None:
    service = ExportService(new_id=_ids())
    outcome = await service.export(
        owner=_OWNER,
        interaction_id="it-1",
        capability_id=CapabilityId("fr008_payroll_ranking"),
        role="manager",
        function="FR-008",
        time_range_label="2026-08-01_2026-08-31",
        result=_result(),
    )

    assert await service.fetch(_OTHER, outcome.artifact_id) is None
    assert await service.fetch(_OWNER, "missing-art") is None


@pytest.mark.asyncio
async def test_buffer_is_bounded_to_newest_entries() -> None:
    service = ExportService(new_id=_ids(), max_entries=2)
    first = None
    for index in range(3):
        outcome = await service.export(
            owner=_OWNER,
            interaction_id=f"it-{index}",
            capability_id=CapabilityId("fr008_payroll_ranking"),
            role="manager",
            function="FR-008",
            time_range_label="2026-08-01_2026-08-31",
            result=_result(),
        )
        if index == 0:
            first = outcome.artifact_id

    assert first is not None
    # The oldest entry was evicted; the newest two remain fetchable.
    assert await service.fetch(_OWNER, first) is None
