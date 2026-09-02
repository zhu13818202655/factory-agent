"""MES category / failure / by-tenant / models / capabilities / errors queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from usage_admin.events import MesCallFact
from usage_admin.ops import OpsQueryError, OpsService
from usage_admin.platform import PlatformRole, PlatformScope, PlatformScopeError
from usage_admin.store import (
    InMemoryUsageStore,
    MesOperationCategory,
    TenantRegistryRecord,
)

NOW = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

VIEWER = PlatformScope("ops-1", PlatformRole.VIEWER, frozenset())
SCOPED = PlatformScope("ops-2", PlatformRole.VIEWER, frozenset({"fac-01"}))


def _mes_fact(
    event_id: str,
    *,
    tenant_id: str = "fac-01",
    operation_id: str,
    status: str = "completed",
    error_category: str | None = None,
    occurred_at: datetime | None = None,
) -> MesCallFact:
    return MesCallFact(
        event_id=event_id,
        tenant_id=tenant_id,
        session_id="session-1",
        interaction_id="interaction-1",
        occurred_at=occurred_at or NOW,
        operation_id=operation_id,
        page_count=1,
        row_count_bucket="1-10",
        duration_ms=200,
        status=status,
        error_category=error_category,
        received_at=NOW,
    )


def build_store() -> InMemoryUsageStore:
    store = InMemoryUsageStore()
    store.mes_call_facts = [
        # fac-01: 2 output (BarcodeClQuery) + 1 payroll (GongziMxQuery) + 1 failed order
        _mes_fact("m-1", tenant_id="fac-01", operation_id="BarcodeClQuery"),
        _mes_fact("m-2", tenant_id="fac-01", operation_id="BarcodeClQuery"),
        _mes_fact("m-3", tenant_id="fac-01", operation_id="GongziMxQuery"),
        _mes_fact(
            "m-4",
            tenant_id="fac-01",
            operation_id="PlanGridPageList",
            status="failed",
            error_category="mes_timeout",
        ),
        # fac-02: 1 other (UserInfoQuery) + 1 failed payroll
        _mes_fact("m-5", tenant_id="fac-02", operation_id="UserInfoQuery"),
        _mes_fact(
            "m-6",
            tenant_id="fac-02",
            operation_id="GongziJeOrderQuery",
            status="failed",
            error_category="unauthorized",
        ),
        # fac-03: unknown operation falls back to "other"
        _mes_fact("m-7", tenant_id="fac-03", operation_id="UnknownOp"),
    ]
    store.tenant_registry = {
        "fac-01": TenantRegistryRecord(
            app_key="fac-01",
            tenant_name="温州一厂",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        ),
        "fac-02": TenantRegistryRecord(
            app_key="fac-02",
            tenant_name="杭州二厂",
            status="disabled",
            created_at=NOW,
            updated_at=NOW,
        ),
    }
    # Override the category mapping to prove the table is consulted when present.
    store.mes_operation_categories = [
        MesOperationCategory(operation_id="BarcodeClQuery", category="output"),
        MesOperationCategory(operation_id="GongziMxQuery", category="payroll"),
        MesOperationCategory(operation_id="PlanGridPageList", category="order"),
        MesOperationCategory(operation_id="UserInfoQuery", category="other"),
        MesOperationCategory(operation_id="GongziJeOrderQuery", category="payroll"),
    ]
    return store


def ops(store: InMemoryUsageStore) -> OpsService:
    return OpsService(store, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_mes_categories_success_only_and_four_buckets() -> None:
    service = ops(build_store())

    view = await service.mes_categories(VIEWER, START, END)

    assert view.categories == {"output": 2, "payroll": 1, "order": 0, "other": 2}
    assert view.total == 5  # successful calls only
    assert view.metric_version.startswith("rollup=")


@pytest.mark.asyncio
async def test_mes_categories_respects_scope() -> None:
    service = ops(build_store())

    view = await service.mes_categories(SCOPED, START, END)

    assert view.categories == {"output": 2, "payroll": 1, "order": 0, "other": 0}
    assert view.tenant_ids == ("fac-01",)


@pytest.mark.asyncio
async def test_mes_failures_are_independent_and_break_down_by_error() -> None:
    service = ops(build_store())

    view = await service.mes_failures(VIEWER, START, END)

    assert view.categories == {"output": 0, "payroll": 1, "order": 1, "other": 0}
    assert view.total == 2
    assert view.by_error == {"mes_timeout": 1, "unauthorized": 1}


@pytest.mark.asyncio
async def test_mes_operations_break_down_by_operation_id() -> None:
    service = ops(build_store())

    view = await service.mes_operations(VIEWER, START, END)

    assert view.values["BarcodeClQuery"] == 2
    assert view.values["UserInfoQuery"] == 1
    assert "PlanGridPageList" not in view.values  # failed call excluded


@pytest.mark.asyncio
async def test_by_tenant_returns_registry_names_statuses_and_counts() -> None:
    store = build_store()
    from support.events import interaction_started
    from usage_admin.store import RollupRow

    store.interaction_facts = [interaction_started("s-1", tenant_id="fac-01", occurred_at=START)]
    store.rollup_rows.append(
        RollupRow(
            tenant_id="fac-01",
            bucket_start=START,
            metric="prompt_tokens",
            value=100,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        )
    )
    store.rollup_rows.append(
        RollupRow(
            tenant_id="fac-01",
            bucket_start=START,
            metric="questions",
            value=1,
            rollup_version="rollup-v2",
            rolled_up_at=NOW,
        )
    )

    page = await OpsService(store, clock=lambda: NOW).by_tenant(
        VIEWER, START, END, limit=10, offset=0
    )

    assert page.total == 3  # fac-01, fac-02 from registry + fac-03 from data
    by_key = {item.app_key: item for item in page.items}
    first = by_key["fac-01"]
    assert first.tenant_name == "温州一厂"
    assert first.status == "active"
    assert first.token_total == 100
    assert first.question_count == 1
    assert first.mes_output == 2
    assert first.mes_payroll == 1
    assert first.last_usage_at is not None
    second = by_key["fac-02"]
    assert second.status == "disabled"
    third = by_key["fac-03"]
    assert third.tenant_name is None  # not registered
    assert third.mes_other == 1


@pytest.mark.asyncio
async def test_by_tenant_name_and_app_key_filters() -> None:
    store = build_store()
    service = ops(store)

    by_name = await service.by_tenant(VIEWER, START, END, limit=10, offset=0, name="温州")
    assert [item.app_key for item in by_name.items] == ["fac-01"]

    by_key = await service.by_tenant(VIEWER, START, END, limit=10, offset=0, app_key="fac-02")
    assert [item.app_key for item in by_key.items] == ["fac-02"]


@pytest.mark.asyncio
async def test_by_tenant_rejects_out_of_scope_filter() -> None:
    store = build_store()
    service = ops(store)

    with pytest.raises(PlatformScopeError, match="exceeds"):
        await service.by_tenant(SCOPED, START, END, limit=10, offset=0, app_key="fac-02")


@pytest.mark.asyncio
async def test_by_tenant_paginates() -> None:
    service = ops(build_store())

    page = await service.by_tenant(VIEWER, START, END, limit=2, offset=0)

    assert len(page.items) == 2
    assert page.total == 3
    assert page.next_cursor == 2


@pytest.mark.asyncio
async def test_models_aggregate_calls_and_tokens() -> None:
    store = InMemoryUsageStore()
    from support.events import llm_call_completed

    store.llm_call_facts = [
        llm_call_completed(
            "l-1",
            actual_model="qwen3-32b",
            prompt_tokens=100,
            tenant_id="fac-01",
            occurred_at=START,
        ),
        llm_call_completed(
            "l-2",
            actual_model="qwen3-32b",
            prompt_tokens=50,
            tenant_id="fac-01",
            occurred_at=START,
        ),
        llm_call_completed(
            "l-3",
            actual_model="deepseek-v3",
            prompt_tokens=10,
            tenant_id="fac-02",
            occurred_at=START,
        ),
    ]
    service = ops(store)

    view = await service.models(VIEWER, START, END)

    assert view.values["qwen3-32b"]["calls"] == 2
    assert view.values["qwen3-32b"]["prompt_tokens"] == 150
    assert view.values["deepseek-v3"]["calls"] == 1


@pytest.mark.asyncio
async def test_capabilities_delegates_to_dimensions() -> None:
    store = InMemoryUsageStore()
    from support.events import interaction_started

    store.interaction_facts = [
        interaction_started("s-1", capability="FR-001", tenant_id="fac-01", occurred_at=START),
        interaction_started("s-2", capability="FR-002", tenant_id="fac-01", occurred_at=START),
    ]
    service = ops(store)

    view = await service.capabilities(VIEWER, START, END)

    assert view.values["FR-001"] == 1
    assert view.values["FR-002"] == 1


@pytest.mark.asyncio
async def test_errors_break_down_across_fact_types() -> None:
    store = InMemoryUsageStore()
    from support.events import interaction_completed, llm_call_completed

    store.interaction_facts = [
        interaction_completed(
            "c-1",
            status="failed",
            error_category="llm_5xx",
            tenant_id="fac-01",
            occurred_at=START,
        )
    ]
    store.llm_call_facts = [
        llm_call_completed(
            "l-1",
            status="failed",
            error_category="llm_timeout",
            tenant_id="fac-01",
            occurred_at=START,
        )
    ]
    service = ops(store)

    view = await service.errors(VIEWER, START, END)

    assert view.values == {"llm_5xx": 1, "llm_timeout": 1}


@pytest.mark.asyncio
async def test_empty_data_returns_zeroed_categories() -> None:
    service = ops(InMemoryUsageStore())

    view = await service.mes_categories(VIEWER, START, END)

    assert view.categories == {"output": 0, "payroll": 0, "order": 0, "other": 0}
    assert view.total == 0
    assert view.incomplete is False


@pytest.mark.asyncio
async def test_over_span_and_inverted_range_are_rejected() -> None:
    service = ops(build_store())
    with pytest.raises(OpsQueryError, match="span budget"):
        await service.mes_categories(VIEWER, END - timedelta(days=400), END)
    with pytest.raises(OpsQueryError, match="start before end"):
        await service.mes_categories(VIEWER, END, START)
