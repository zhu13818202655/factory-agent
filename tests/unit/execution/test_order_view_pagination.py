from dataclasses import replace
from typing import Any, cast

import pytest

from factory_agent.data_api.result_cache import ResultCacheExecutor, ResultCacheMode
from factory_agent.domain import Role
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.kernel import KernelCapabilityRunner
from factory_agent.execution.recipes import RecipeRegistry
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.ports.order_views import OrderViewSelection
from tests.unit.execution.test_kernel_shared_cache import (
    TODAY,
    NoMesExecutor,
    plan_group_contribution,
    request_for,
    runner,
    seed_snapshots,
)


def sheets(count: int, *, separate_plans: bool = False) -> tuple[dict[str, object], ...]:
    return tuple(
        plan_group_contribution(
            f"20260701{index:04d}",
            f"PLAN-{index:03d}" if separate_plans else None,
            "STYLE-SYNTHETIC",
            str(index),
            "D1",
            "10",
            "10" if index % 2 else "5",
            "1",
            "1",
            f"WORK-{index}",
            rq="2026-07-08",
        )
        for index in range(count)
    )


@pytest.mark.asyncio
async def test_warm_detail_pages_keep_snapshot_status_and_complete_filtered_export() -> None:
    cache, _ = await seed_snapshots(ResultCacheMode.READ, progress_rows=sheets(23))
    executor = NoMesExecutor()
    kernel = runner(ResultCacheExecutor(executor, cache))
    request = request_for("FR-009", "fr009_factory_order_overview", Role.OWNER)
    overview = await kernel.run(request)
    card = cast(dict[str, Any], overview.card)
    snapshot = card["data_snapshot"]["id"]
    selection = OrderViewSelection(
        view_id="sheet_details",
        group_kind="unlinked",
        page_size=3,
        status="unfinished",
        snapshot_id=snapshot,
    )
    first = await kernel.run(replace(request, order_view=selection))
    first_view = cast(dict[str, Any], first.card)["order_groups"]["groups"][0]["views"][0]
    second = await kernel.run(replace(request, order_view=replace(selection, page=2)))
    second_view = cast(dict[str, Any], second.card)["order_groups"]["groups"][0]["views"][0]
    assert first_view["table"]["filtered_total_rows"] == 12
    assert second_view["table"]["page"] == 2
    assert second_view["request"]["order_view"]["status"] == "unfinished"
    assert second_view["request"]["order_view"]["page"] == 2
    assert second_view["request"]["order_view"]["snapshot_id"] == snapshot
    assert first_view["table"]["rows"] != second_view["table"]["rows"]
    assert len(second.detail_tables[0].rows) == 12
    assert second.api_call_count == 0
    assert executor.calls == []


@pytest.mark.asyncio
async def test_warm_group_list_pages_do_not_omit_groups_from_export() -> None:
    cache, _ = await seed_snapshots(
        ResultCacheMode.READ, progress_rows=sheets(63, separate_plans=True)
    )
    executor = NoMesExecutor()
    kernel = runner(ResultCacheExecutor(executor, cache))
    request = replace(
        request_for("FR-009", "fr009_factory_order_overview", Role.OWNER), group_page_size=20
    )
    first = await kernel.run(request)
    card = cast(dict[str, Any], first.card)
    second = await kernel.run(
        replace(request, group_page=2, data_snapshot_id=card["data_snapshot"]["id"])
    )
    second_groups = cast(dict[str, Any], second.card)["order_groups"]
    assert second_groups["page"] == 2
    assert second_groups["groups_total"] == 63
    assert len(second_groups["groups"]) == 20
    assert {group["id"] for group in card["order_groups"]["groups"]}.isdisjoint(
        {group["id"] for group in second_groups["groups"]}
    )
    assert len(second.rows) == len(second.detail_tables[0].rows) == 63
    assert executor.calls == []


@pytest.mark.parametrize("refreshed", [(), sheets(1)])
@pytest.mark.asyncio
async def test_same_timestamp_refresh_rejects_old_detail_snapshot_even_when_empty(
    refreshed: tuple[dict[str, object], ...],
) -> None:
    cache, _ = await seed_snapshots(ResultCacheMode.READ, progress_rows=sheets(23))
    executor = NoMesExecutor()
    kernel = runner(ResultCacheExecutor(executor, cache))
    request = request_for("FR-009", "fr009_factory_order_overview", Role.OWNER)
    result = await kernel.run(request)
    snapshot = cast(dict[str, Any], result.card)["data_snapshot"]["id"]
    assert await cache.write(
        request.filters.tenant_id,
        "ScjdQuery",
        refreshed,
        None,
        {},
        cache.prefetch_window(TODAY.date().isoformat()),
        source="precompute",
    )
    with pytest.raises(InvalidRequestError, match="order_view_snapshot_changed"):
        await kernel.run(
            replace(
                request,
                order_view=OrderViewSelection(
                    view_id="sheet_details",
                    group_kind="unlinked",
                    page=2,
                    page_size=3,
                    snapshot_id=snapshot,
                ),
            )
        )
    assert executor.calls == []


@pytest.mark.asyncio
async def test_kernel_rejects_missing_pagination_snapshot_before_any_fetch() -> None:
    executor = NoMesExecutor()
    kernel = runner(executor)
    request = request_for("FR-009", "fr009_factory_order_overview", Role.OWNER)
    for invalid in (
        replace(request, group_page=2),
        replace(
            request,
            order_view=OrderViewSelection(view_id="sheet_details", group_kind="unlinked", page=2),
        ),
    ):
        with pytest.raises(InvalidRequestError, match="order_view_invalid"):
            await kernel.run(invalid)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_recipe_change_invalidates_snapshot_without_global_registry_version_change() -> None:
    cache, _ = await seed_snapshots(ResultCacheMode.READ, progress_rows=sheets(23))
    executor = NoMesExecutor()
    cache_executor = ResultCacheExecutor(executor, cache)
    kernel = runner(cache_executor)
    request = request_for("FR-009", "fr009_factory_order_overview", Role.OWNER)
    result = await kernel.run(request)
    snapshot = cast(dict[str, Any], result.card)["data_snapshot"]["id"]
    registry = kernel.recipes
    recipe = registry.get(str(request.capability_id))
    assert recipe.card is not None
    updated = recipe.model_copy(
        update={
            "card": recipe.card.model_copy(update={"notes": (*recipe.card.notes, "新的审核口径")})
        }
    )
    changed_registry = RecipeRegistry(
        version=registry.version,
        _recipes={
            capability: updated
            if capability == str(request.capability_id)
            else registry.get(capability)
            for capability in registry.capability_ids
        },
    )
    changed_kernel = KernelCapabilityRunner(
        cache_executor, changed_registry, default_metric_registry(), clock=lambda: TODAY
    )
    with pytest.raises(InvalidRequestError, match="order_view_snapshot_changed"):
        await changed_kernel.run(
            replace(
                request,
                order_view=OrderViewSelection(
                    view_id="sheet_details",
                    group_kind="unlinked",
                    page=2,
                    snapshot_id=snapshot,
                ),
            )
        )
    assert executor.calls == []
