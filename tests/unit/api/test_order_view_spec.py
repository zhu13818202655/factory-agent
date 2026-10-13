from typing import Any

import pytest
from pydantic import ValidationError

from factory_agent.api.sessions import DrillSpec, OrderViewSpec
from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain import Role
from factory_agent.execution.recipes import load_recipes
from factory_agent.ports.order_views import OrderViewSelection
from tests.support.session import InMemoryInteractionStore, RecordingCapabilityRunner
from tests.unit.api.test_sessions_api import HEADERS, client
from tests.unit.application.test_session_service import ECHOED_END, ECHOED_START


def order_view_body(**updates: object) -> dict[str, object]:
    return {"view_id": "sheet_details", "group_kind": "unlinked", **updates}


def drill_body(**updates: object) -> dict[str, object]:
    return {
        "capability_id": "fr009_factory_order_overview",
        "time_range_start": ECHOED_START.isoformat(),
        "time_range_end": ECHOED_END.isoformat(),
        "order_view": order_view_body(),
        **updates,
    }


def test_api_order_view_selection_keeps_missing_key_distinct_from_display_label() -> None:
    unlinked = OrderViewSpec.model_validate(order_view_body()).to_selection()
    assert unlinked == OrderViewSelection(view_id="sheet_details", group_kind="unlinked")
    linked = OrderViewSpec.model_validate(
        order_view_body(group_kind="plan", plan_code="未关联计划号")
    ).to_selection()
    assert linked.group_kind == "plan"
    assert linked.plan_code == "未关联计划号"
    assert linked != unlinked


def test_api_drill_conversion_preserves_business_narrowing_and_original_window() -> None:
    body = drill_body(
        dept_ids=["dept-1"],
        employee_uid="emp-1",
        order_codes=["SHEET-SYNTHETIC"],
        style_codes=["STYLE-SYNTHETIC"],
        plan_codes=["PLAN-SYNTHETIC"],
        material_ids=["MATERIAL-SYNTHETIC"],
        time_expression="本月",
        order_view=order_view_body(page=2, page_size=25, status="unfinished", snapshot_id="a" * 64),
    )
    payload = DrillSpec.model_validate(body).to_payload()
    assert payload.dept_ids == ("dept-1",)
    assert payload.employee_uid == "emp-1"
    assert payload.order_codes == ("SHEET-SYNTHETIC",)
    assert payload.style_codes == ("STYLE-SYNTHETIC",)
    assert payload.plan_codes == ("PLAN-SYNTHETIC",)
    assert payload.material_ids == ("MATERIAL-SYNTHETIC",)
    assert payload.time_expression == "本月"
    assert (payload.time_range_start, payload.time_range_end) == (ECHOED_START, ECHOED_END)
    assert payload.order_view is not None
    assert payload.order_view.page == 2
    assert payload.order_view.page_size == 25
    assert payload.order_view.status == "unfinished"
    assert payload.order_view.snapshot_id == "a" * 64


def test_api_group_pagination_conversion_keeps_snapshot_and_business_filters() -> None:
    payload = DrillSpec.model_validate(
        drill_body(
            order_view=None,
            group_page=2,
            group_page_size=25,
            data_snapshot_id="b" * 64,
            style_codes=["STYLE-SYNTHETIC"],
        )
    ).to_payload()
    assert payload.order_view is None
    assert payload.group_page == 2
    assert payload.group_page_size == 25
    assert payload.data_snapshot_id == "b" * 64
    assert payload.style_codes == ("STYLE-SYNTHETIC",)
    assert (payload.time_range_start, payload.time_range_end) == (ECHOED_START, ECHOED_END)


@pytest.mark.parametrize(
    "updates",
    [
        {"group_page": True},
        {"group_page": 2},
        {"group_page": 0},
        {"group_page_size": True},
        {"group_page_size": 51},
        {"group_page": 2, "data_snapshot_id": "b" * 64, "order_view": order_view_body()},
        {"data_snapshot_id": "invalid"},
        {
            "group_page": 2,
            "data_snapshot_id": "b" * 64,
            "time_range_start": None,
            "time_range_end": None,
        },
    ],
)
def test_api_group_pagination_rejects_invalid_bounds_or_mixed_selection(
    updates: dict[str, object],
) -> None:
    body = drill_body(order_view=None)
    body.update(updates)
    with pytest.raises(ValidationError):
        DrillSpec.model_validate(body)


@pytest.mark.parametrize(
    "updates",
    [
        {"page": True},
        {"page": "2", "snapshot_id": "a" * 64},
        {"page": 2},
        {"page": 0},
        {"page": 100001, "snapshot_id": "a" * 64},
        {"page_size": False},
        {"page_size": 101},
        {"snapshot_id": "not-a-snapshot"},
        {"snapshot_id": "a" * 63},
        {"plan_code": "未关联计划号"},
        {"group_kind": "plan"},
        {"group_kind": "plan", "plan_code": " "},
        {"status": "done"},
        {"view_id": "../sheet_details"},
        {"tenant_id": "different-tenant"},
        {"employee_ids": ["outside-scope"]},
    ],
)
def test_api_order_view_rejects_invalid_pagination_keys_and_scope_fields(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OrderViewSpec.model_validate(order_view_body(**updates))


@pytest.mark.parametrize("missing", ["start", "end", "both"])
def test_order_view_requires_both_absolute_window_boundaries(missing: str) -> None:
    body = drill_body(time_expression="上个月")
    if missing in {"start", "both"}:
        body.pop("time_range_start")
    if missing in {"end", "both"}:
        body.pop("time_range_end")
    with pytest.raises(ValidationError):
        DrillSpec.model_validate(body)


@pytest.mark.parametrize("field", ["order_codes", "style_codes", "plan_codes", "material_ids"])
@pytest.mark.parametrize("value", [[""], ["x" * 129], ["x"] * 21, [True]])
def test_api_drill_business_codes_are_bounded_strings(field: str, value: list[object]) -> None:
    with pytest.raises(ValidationError):
        DrillSpec.model_validate(drill_body(**{field: value}))


@pytest.mark.parametrize(
    "body",
    [
        drill_body(order_view=order_view_body(page=True)),
        drill_body(order_view=order_view_body(page=2)),
        drill_body(order_view=order_view_body(snapshot_id="invalid")),
        drill_body(order_view=order_view_body(plan_code="未关联计划号")),
        {
            "capability_id": "fr009_factory_order_overview",
            "order_view": order_view_body(),
            "time_expression": "上个月",
        },
    ],
)
@pytest.mark.asyncio
async def test_http_invalid_order_view_does_not_persist_or_execute(body: dict[str, Any]) -> None:
    store = InMemoryInteractionStore()
    runner = RecordingCapabilityRunner()
    runner.recipes = load_recipes(load_catalog().operation_ids)
    async with client(store, runner, role=Role.OWNER) as http:
        response = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "展开订单", "drill": body},
            headers=HEADERS,
        )
    assert response.status_code == 422
    assert store.interactions == {}
    assert store.messages == []
    assert runner.requests == []


@pytest.mark.parametrize(
    ("capability", "view"),
    [
        ("fr009_factory_order_overview", order_view_body(view_id="unknown_view")),
        ("fr006_order_output", order_view_body(view_id="worktype_details", status="unfinished")),
    ],
)
@pytest.mark.asyncio
async def test_http_unreviewed_view_or_status_does_not_persist_or_execute(
    capability: str, view: dict[str, object]
) -> None:
    store = InMemoryInteractionStore()
    runner = RecordingCapabilityRunner()
    runner.recipes = load_recipes(load_catalog().operation_ids)
    async with client(store, runner, role=Role.OWNER) as http:
        response = await http.post(
            "/v1/sessions/session-1/interactions",
            json={
                "text": "展开订单",
                "drill": drill_body(capability_id=capability, order_view=view),
            },
            headers=HEADERS,
        )
    assert response.status_code == 400
    assert store.interactions == {}
    assert runner.requests == []


@pytest.mark.asyncio
async def test_http_order_group_page_reaches_authorized_capability_without_text_routing() -> None:
    store = InMemoryInteractionStore()
    runner = RecordingCapabilityRunner()
    runner.recipes = load_recipes(load_catalog().operation_ids)
    body = drill_body(
        order_view=order_view_body(page=2, snapshot_id="a" * 64), style_codes=["STYLE-SYNTHETIC"]
    )
    async with client(store, runner, role=Role.OWNER) as http:
        created = await http.post(
            "/v1/sessions/session-1/interactions",
            json={"text": "展开下一页", "drill": body},
            headers=HEADERS,
        )
        assert created.status_code == 201
        interaction_id = created.json()["interaction_id"]
        response = await http.get(f"/v1/interactions/{interaction_id}/stream", headers=HEADERS)
    assert response.status_code == 200
    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert str(request.capability_id) == "fr009_factory_order_overview"
    assert request.role is Role.OWNER
    assert request.filters.style_codes == frozenset({"STYLE-SYNTHETIC"})
    assert request.order_view is not None
    assert request.order_view.page == 2
    assert request.order_view.group_kind == "unlinked"
    assert request.order_view.plan_code is None
    assert request.order_view.snapshot_id == "a" * 64
    assert (request.time_range.start, request.time_range.end) == (ECHOED_START, ECHOED_END)
