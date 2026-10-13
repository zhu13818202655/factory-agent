from decimal import Decimal
from typing import cast

import pytest

from factory_agent.ports.card import CardAction, CardColumn
from factory_agent.ports.order_views import (
    OrderDetailData,
    OrderGroupKind,
    OrderViewSelection,
    build_order_groups,
)
from tests.support.payload import as_dict, as_list

_SUMMARY_COLUMNS = (
    CardColumn("plan_code"),
    CardColumn("plan_missing"),
    CardColumn("order_code", title="订单号"),
    CardColumn("order_count", column_type="quantity"),
    CardColumn("sheet_count", title="制单数", column_type="quantity", unit="单"),
    CardColumn("progress_ratio", title="进度", column_type="percent", unit="%"),
    CardColumn("finished_sheet_count", column_type="quantity"),
    CardColumn("unfinished_sheet_count", column_type="quantity"),
    CardColumn("unavailable_sheet_count", column_type="quantity"),
)
_DETAIL_COLUMNS = (
    CardColumn("plan_code"),
    CardColumn("order_code", title="订单号"),
    CardColumn("sheet_code", title="制单号"),
    CardColumn("style_code", title="款号"),
    CardColumn("output_qty", title="报工工作量", column_type="quantity", unit="件·工序"),
    CardColumn("output_share", title="工序占比", column_type="percent", unit="%"),
    CardColumn("finish_state", title="状态"),
)


def _summary(plan_code: str | None = None) -> dict[str, object]:
    return {
        "plan_code": plan_code,
        "plan_missing": int(plan_code is None),
        "order_code": plan_code or "未关联计划号",
        "order_count": Decimal("1"),
        "sheet_count": Decimal("3"),
        "progress_ratio": Decimal("99.994"),
        "finished_sheet_count": Decimal("1"),
        "unfinished_sheet_count": Decimal("1"),
        "unavailable_sheet_count": Decimal("1"),
    }


def _detail_row(
    index: int, *, plan_code: str | None = None, state: object = "已完工"
) -> dict[str, object]:
    return {
        "plan_code": plan_code,
        "order_code": plan_code or "未关联计划号",
        "sheet_code": f"S{index:03}",
        "style_code": "STYLE-A",
        "output_qty": Decimal(f"{index}.123"),
        "output_share": "unavailable",
        "finish_state": state,
        "uid": "not-a-declared-column",
        "sensitive_canary": "must-not-enter-card",
    }


def _details(*rows: dict[str, object]) -> OrderDetailData:
    return OrderDetailData(
        "sheets",
        "制单明细",
        "plan_code",
        _DETAIL_COLUMNS,
        tuple(rows),
        status_column="finish_state",
    )


def _group(payload: dict[str, object], index: int = 0) -> dict[str, object]:
    return as_dict(as_list(payload["groups"])[index])


def _table(payload: dict[str, object]) -> dict[str, object]:
    return as_dict(as_dict(as_list(_group(payload)["views"])[0])["table"])


def test_group_summary_uses_reviewed_values_and_hides_group_metadata() -> None:
    payload = build_order_groups(
        "fr009_factory_order_overview", (_summary(),), _SUMMARY_COLUMNS, (_details(_detail_row(1)),)
    )
    group = _group(payload)
    assert group["id"] == "unlinked"
    assert group["kind"] == "unlinked"
    assert group["plan_code"] is None
    assert group["name"] == "未关联计划号"
    assert group["default_expanded"] is True
    assert group["state_counts"] == {"finished": "1", "unfinished": "1", "unavailable": "1"}
    metrics = as_list(group["summary"])
    assert metrics[0] == {
        "name": "sheet_count",
        "title": "制单数",
        "type": "quantity",
        "unit": "单",
        "value": "3",
    }
    assert as_dict(metrics[1])["value"] == "99.99"
    assert not {"plan_code", "plan_missing", "order_count", "order_code"}.intersection(
        as_dict(item)["name"] for item in metrics
    )
    assert "must-not-enter-card" not in str(payload)
    assert "not-a-declared-column" not in str(payload)


def test_page_two_slices_full_group_without_changing_original_or_counts() -> None:
    rows = tuple(_detail_row(index) for index in range(1, 26))
    payload = build_order_groups(
        "fr009_factory_order_overview",
        (_summary(),),
        _SUMMARY_COLUMNS,
        (_details(*rows),),
        OrderViewSelection("sheets", "unlinked", page=2, page_size=10),
    )
    table = _table(payload)
    assert table["page"] == 2
    assert table["page_size"] == 10
    assert table["total_rows"] == table["filtered_total_rows"] == 25
    assert table["has_more"] is True
    assert as_list(table["rows"])[0] == ["S011", "STYLE-A", "11.12", "已完工"]
    assert as_list(table["rows"])[-1] == ["S020", "STYLE-A", "20.12", "已完工"]
    assert len(rows) == 25
    view = as_dict(as_list(_group(payload)["views"])[0])
    assert view["request"] == {
        "capability_id": "fr009_factory_order_overview",
        "order_view": {
            "view_id": "sheets",
            "group_kind": "unlinked",
            "plan_code": None,
            "page": 2,
            "page_size": 10,
            "status": "all",
        },
    }


def test_status_filter_is_applied_before_paging_and_reports_both_counts() -> None:
    payload = build_order_groups(
        "fr009_factory_order_overview",
        (_summary(),),
        _SUMMARY_COLUMNS,
        (
            _details(
                _detail_row(1),
                _detail_row(2, state="未完工"),
                _detail_row(3, state="无法判断"),
                _detail_row(4, state="未完工"),
            ),
        ),
        OrderViewSelection("sheets", "unlinked", page=2, page_size=1, status="unfinished"),
    )
    table = _table(payload)
    assert table["total_rows"] == 4
    assert table["filtered_total_rows"] == 2
    assert table["status"] == "unfinished"
    assert table["rows"] == [["S004", "STYLE-A", "4.12", "未完工"]]
    assert table["has_more"] is False
    view = as_dict(as_list(_group(payload)["views"])[0])
    request = as_dict(view["request"])
    echoed_selection = as_dict(request["order_view"])
    assert echoed_selection["status"] == "unfinished"
    assert echoed_selection["page"] == 2


@pytest.mark.parametrize("missing", [None, "", " \t "])
def test_missing_plan_variants_share_unlinked_identity(missing: str | None) -> None:
    summary = _summary()
    summary["plan_code"] = missing
    payload = build_order_groups(
        "fr009_factory_order_overview",
        (summary,),
        _SUMMARY_COLUMNS,
        (
            _details(
                _detail_row(1, plan_code=None),
                _detail_row(2, plan_code=""),
                _detail_row(3, plan_code=" \t"),
            ),
        ),
    )
    assert _group(payload)["id"] == "unlinked"
    assert _table(payload)["total_rows"] == 3


def test_same_display_label_does_not_merge_real_plan_with_unlinked_group() -> None:
    real = _summary("未关联计划号")
    payload = build_order_groups(
        "fr009_factory_order_overview",
        (_summary(), real),
        _SUMMARY_COLUMNS,
        (_details(_detail_row(1), _detail_row(2, plan_code="未关联计划号")),),
    )
    groups = as_list(payload["groups"])
    assert len(groups) == 2
    assert as_dict(groups[0])["name"] == as_dict(groups[1])["name"]
    assert as_dict(groups[0])["id"] != as_dict(groups[1])["id"]
    assert as_dict(groups[1])["kind"] == "plan"
    assert as_dict(groups[1])["plan_code"] == "未关联计划号"
    assert _table(payload)["total_rows"] == 1
    assert all(as_dict(group)["default_expanded"] is False for group in groups)


def test_nonempty_plan_spaces_remain_part_of_exact_group_key() -> None:
    payload = build_order_groups(
        "fr006_order_output",
        (_summary(" P-1 "), _summary("P-1")),
        _SUMMARY_COLUMNS,
        (_details(_detail_row(1, plan_code=" P-1 "), _detail_row(2, plan_code="P-1")),),
        OrderViewSelection("sheets", "plan", plan_code=" P-1 "),
    )
    assert payload["groups_total"] == 2
    assert len(as_list(payload["groups"])) == 1
    assert _group(payload)["plan_code"] == " P-1 "
    assert _table(payload)["rows"] == [["S001", "STYLE-A", "1.12", "已完工"]]


def test_all_unavailable_column_is_hidden_but_explicitly_described() -> None:
    payload = build_order_groups(
        "fr006_order_output", (_summary(),), _SUMMARY_COLUMNS, (_details(_detail_row(1)),)
    )
    table = _table(payload)
    assert table["columns"] == ["sheet_code", "style_code", "output_qty", "finish_state"]
    assert table["unavailable_columns"] == [
        {
            "name": "output_share",
            "title": "工序占比",
            "type": "percent",
            "unit": "%",
        }
    ]
    metadata = as_list(table["column_metadata"])
    assert as_dict(metadata[2])["unit"] == "件·工序"


def test_unavailable_column_is_checked_across_full_group_not_preview() -> None:
    first = _detail_row(1)
    second = _detail_row(2)
    second["output_share"] = Decimal("18.75")
    payload = build_order_groups(
        "fr006_order_output",
        (_summary(),),
        _SUMMARY_COLUMNS,
        (_details(first, second),),
        preview_rows=1,
    )
    table = _table(payload)
    assert "output_share" in as_list(table["columns"])
    assert table["unavailable_columns"] == []
    assert table["has_more"] is True
    assert as_list(as_list(table["rows"])[0])[-2] is None


def test_hidden_detail_columns_never_enter_schema_or_rows() -> None:
    detail = OrderDetailData(
        "sheets",
        "制单明细",
        "plan_code",
        _DETAIL_COLUMNS,
        (_detail_row(1),),
        hidden_columns=("style_code", "finish_state"),
    )
    payload = build_order_groups(
        "fr009_factory_order_overview", (_summary(),), _SUMMARY_COLUMNS, (detail,)
    )
    assert _table(payload)["columns"] == ["sheet_code", "output_qty"]
    assert _table(payload)["rows"] == [["S001", "1.12"]]


def test_empty_result_and_empty_detail_have_honest_counts() -> None:
    empty = build_order_groups("fr009_factory_order_overview", (), _SUMMARY_COLUMNS, ())
    assert empty == {
        "version": 1,
        "groups_total": 0,
        "page": 1,
        "page_size": 50,
        "has_more": False,
        "groups": [],
    }
    payload = build_order_groups(
        "fr009_factory_order_overview", (_summary(),), _SUMMARY_COLUMNS, (_details(),)
    )
    table = _table(payload)
    assert table["rows"] == []
    assert table["total_rows"] == table["filtered_total_rows"] == 0
    assert table["has_more"] is False
    assert table["unavailable_columns"] == []


def test_group_and_preview_caps_bound_payload_without_losing_total_counts() -> None:
    summaries = tuple(_summary(f"P{index}") for index in range(60))
    rows = tuple(
        _detail_row(row_index, plan_code=f"P{group_index}")
        for group_index in range(60)
        for row_index in range(1, 21)
    )
    payload = build_order_groups(
        "fr009_factory_order_overview", summaries, _SUMMARY_COLUMNS, (_details(*rows),)
    )
    assert payload["groups_total"] == 60
    assert payload["has_more"] is True
    groups = as_list(payload["groups"])
    assert len(groups) == 50
    for group in groups:
        view = as_dict(as_list(as_dict(group)["views"])[0])
        table = as_dict(view["table"])
        assert table["total_rows"] == 20
        assert len(as_list(table["rows"])) == 10


@pytest.mark.parametrize(
    "selection",
    [
        OrderViewSelection("sheets", "plan", plan_code="UNKNOWN"),
        OrderViewSelection("unknown_view", "unlinked"),
    ],
)
def test_unknown_selection_fails_without_returning_broader_rows(
    selection: OrderViewSelection,
) -> None:
    with pytest.raises(ValueError, match="unavailable in this result"):
        build_order_groups(
            "fr009_factory_order_overview",
            (_summary(),),
            _SUMMARY_COLUMNS,
            (_details(_detail_row(1)),),
            selection,
        )


def test_view_without_status_column_rejects_status_filter() -> None:
    detail = OrderDetailData("worktypes", "工序明细", "plan_code", _DETAIL_COLUMNS, ())
    with pytest.raises(ValueError, match="does not support status"):
        build_order_groups(
            "fr006_order_output",
            (_summary(),),
            _SUMMARY_COLUMNS,
            (detail,),
            OrderViewSelection("worktypes", "unlinked", status="unfinished"),
        )


@pytest.mark.parametrize("value", [True, False, 0, -1, 100_001, "2"])
def test_selection_rejects_noninteger_and_unbounded_page(value: object) -> None:
    with pytest.raises(ValueError, match="Invalid order page"):
        OrderViewSelection("sheets", "unlinked", page=cast(int, value))


@pytest.mark.parametrize("value", [True, 0, 101, "10"])
def test_selection_rejects_noninteger_and_unbounded_page_size(value: object) -> None:
    with pytest.raises(ValueError, match="Invalid order page size"):
        OrderViewSelection("sheets", "unlinked", page_size=cast(int, value))


@pytest.mark.parametrize("view_id", ["", "../../canary", "x y", "sheets\n", "a" * 65])
def test_selection_rejects_invalid_view_identifier_without_echo(view_id: str) -> None:
    with pytest.raises(ValueError, match="^Invalid order view identifier$"):
        OrderViewSelection(view_id, "unlinked")


@pytest.mark.parametrize("snapshot_id", ["secret-canary", "f" * 63, "g" * 64, " f" * 32])
def test_selection_rejects_invalid_snapshot_identifier_without_echo(snapshot_id: str) -> None:
    with pytest.raises(ValueError, match="^Invalid order snapshot identifier$"):
        OrderViewSelection("sheets", "unlinked", snapshot_id=snapshot_id)


def test_selection_validates_kind_and_keeps_valid_snapshot() -> None:
    selection = OrderViewSelection("sheets", "plan", plan_code=" P1 ", snapshot_id="a" * 64)
    assert selection.plan_code == " P1 "
    assert selection.snapshot_id == "a" * 64
    with pytest.raises(ValueError, match="requires a plan"):
        OrderViewSelection("sheets", "plan", plan_code=" \t")
    with pytest.raises(ValueError, match="cannot carry"):
        OrderViewSelection("sheets", "unlinked", plan_code="P1")
    with pytest.raises(ValueError, match="Unsupported order group"):
        OrderViewSelection("sheets", cast(OrderGroupKind, "bad"))


def test_duplicate_group_summaries_are_rejected_instead_of_reaggregated() -> None:
    with pytest.raises(ValueError, match="Duplicate order summary group"):
        build_order_groups(
            "fr009_factory_order_overview", (_summary(), _summary()), _SUMMARY_COLUMNS, ()
        )


@pytest.mark.parametrize("name,value", [("max_groups", 51), ("preview_rows", 101)])
def test_payload_limits_have_hard_upper_bounds(name: str, value: int) -> None:
    with pytest.raises(ValueError, match="Invalid order"):
        if name == "max_groups":
            build_order_groups("fr009_factory_order_overview", (), (), (), max_groups=value)
        else:
            build_order_groups("fr009_factory_order_overview", (), (), (), preview_rows=value)


def test_detail_actions_declare_reviewed_drill_bindings() -> None:
    detail = OrderDetailData(
        "sheets",
        "制单明细",
        "plan_code",
        _DETAIL_COLUMNS,
        (_detail_row(1),),
        actions=(
            CardAction("查看包明细", "fr005_order_package_detail", {"order_codes": "sheet_code"}),
        ),
    )
    payload = build_order_groups(
        "fr009_factory_order_overview", (_summary(),), _SUMMARY_COLUMNS, (detail,)
    )
    assert _table(payload)["actions"] == [
        {
            "type": "drill",
            "label": "查看包明细",
            "capability_id": "fr005_order_package_detail",
            "bind_from": {"order_codes": "sheet_code"},
        }
    ]


def test_detail_actions_include_only_declared_business_field_clears() -> None:
    row = _detail_row(1)
    row["material_id"] = "MATERIAL-A"
    detail = OrderDetailData(
        "packages",
        "包明细",
        "plan_code",
        (*_DETAIL_COLUMNS, CardColumn("material_id", title="物料编号")),
        (row,),
        actions=(
            CardAction(
                "查看工序",
                "fr005_order_worktype_detail",
                {"material_ids": "material_id"},
                clear_fields=("order_codes",),
            ),
        ),
    )
    payload = build_order_groups(
        "fr009_factory_order_overview",
        (_summary(),),
        _SUMMARY_COLUMNS,
        (detail,),
    )
    action = as_dict(as_list(_table(payload)["actions"])[0])
    assert action["clear_fields"] == ["order_codes"]
    assert action["bind_from"] == {"material_ids": "material_id"}


def test_detail_missing_group_key_is_rejected_before_payload_projection() -> None:
    row = _detail_row(1)
    del row["plan_code"]
    with pytest.raises(ValueError, match="Order detail is missing its group key"):
        build_order_groups(
            "fr009_factory_order_overview", (_summary(),), _SUMMARY_COLUMNS, (_details(row),)
        )


def test_second_group_page_returns_remaining_groups_with_full_counts() -> None:
    summaries = tuple(_summary(f"P{index}") for index in range(60))
    payload = build_order_groups(
        "fr009_factory_order_overview",
        summaries,
        _SUMMARY_COLUMNS,
        (),
        group_page=2,
    )
    assert payload["page"] == 2
    assert payload["page_size"] == 50
    assert payload["groups_total"] == 60
    assert payload["has_more"] is False
    groups = as_list(payload["groups"])
    assert len(groups) == 10
    assert as_dict(groups[0])["plan_code"] == "P50"
    assert as_dict(groups[-1])["plan_code"] == "P59"


@pytest.mark.parametrize("page", [True, 0, -1, 100001, "2"])
def test_group_page_requires_a_bounded_strict_integer(page: object) -> None:
    with pytest.raises(ValueError, match="Invalid order group page"):
        build_order_groups("fr009_factory_order_overview", (), (), (), group_page=cast(int, page))


def test_group_page_and_detail_selection_cannot_be_combined() -> None:
    with pytest.raises(ValueError, match="cannot paginate the group list"):
        build_order_groups(
            "fr009_factory_order_overview",
            (_summary(),),
            _SUMMARY_COLUMNS,
            (_details(_detail_row(1)),),
            OrderViewSelection("sheets", "unlinked"),
            group_page=2,
        )
