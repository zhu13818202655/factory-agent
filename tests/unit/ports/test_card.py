"""Card builder tests: numbers only from the result table, no fabrication."""

from decimal import Decimal

from factory_agent.ports.card import CardAlertSpec, CardColumn, CardTableSpec, build_card

_WAGE_COLUMNS = (
    CardColumn("gross_total", title="计件工资合计", column_type="money", unit="元"),
    CardColumn("piece_count", title="计件件数", column_type="quantity", unit="件"),
    CardColumn("daily_avg", title="日均工资", column_type="money", unit="元"),
)


def _kpi_spec() -> CardTableSpec:
    return CardTableSpec(kind="kpi", metrics=("gross_total", "piece_count", "daily_avg"))


def test_kpi_card_resolves_values_from_totals_and_rows() -> None:
    card = build_card(
        _kpi_spec(),
        capability_id="fr002_personal_wage_summary",
        title="个人工资汇总",
        columns=_WAGE_COLUMNS,
        rows=(
            {"gross_total": Decimal("21.65"), "piece_count": Decimal("20"), "daily_avg": "0.35"},
        ),
        totals={"gross_total": Decimal("21.65"), "piece_count": Decimal("20")},
    )

    assert card["kind"] == "kpi"
    metrics = card["metrics"]
    assert metrics[0] == {"label": "计件工资合计", "unit": "元", "value": "21.65"}
    # totals take precedence over the row value; a formatted string cell is fine
    assert metrics[2] == {"label": "日均工资", "unit": "元", "value": "0.35"}
    assert "table" not in card


def test_kpi_metric_without_data_source_is_explicit_not_zero() -> None:
    card = build_card(
        _kpi_spec(),
        capability_id="fr002_personal_wage_summary",
        title="个人工资汇总",
        columns=_WAGE_COLUMNS,
        rows=(
            {
                "gross_total": Decimal("21.65"),
                "piece_count": Decimal("20"),
                "daily_avg": "unavailable",
            },
        ),
        totals={"gross_total": Decimal("21.65")},
    )

    metrics = card["metrics"]
    assert metrics[2]["unavailable"] is True
    assert card["unavailable_columns"] == ["日均工资"]


def test_table_card_truncates_preview_rows() -> None:
    spec = CardTableSpec(kind="table", preview_max_rows=2)
    rows = tuple({"rq": f"2026-09-0{i}", "je": Decimal("10")} for i in range(1, 6))
    card = build_card(
        spec,
        capability_id="fr003_personal_wage_detail",
        title="个人工资明细",
        columns=(
            CardColumn("rq", title="日期", column_type="date"),
            CardColumn("je", title="小计金额", column_type="money", unit="元"),
        ),
        rows=rows,
        totals={},
    )

    table = card["table"]
    assert table["columns"] == ["rq", "je"]
    assert table["column_titles"] == {"rq": "日期", "je": "小计金额"}
    assert table["total_rows"] == 5
    assert table["preview_max_rows"] == 2
    assert table["truncated"] is True
    assert table["rows"] == [
        ["2026-09-01", "10"],
        ["2026-09-02", "10"],
    ]


def test_grouped_card_keeps_group_runs_contiguous_and_caps_rows_per_group() -> None:
    spec = CardTableSpec(
        kind="ranking",
        metrics=("gross",),
        totals=("gross",),
        preview_max_rows=2,
        preview_max_groups=50,
        group_by="dept",
        rank_column="rank_position",
    )
    rows = (
        {"uname": "甲", "dept": "001", "gross": "100", "rank_position": "1"},
        {"uname": "乙", "dept": "001", "gross": "90", "rank_position": "2"},
        {"uname": "丙", "dept": "001", "gross": "80", "rank_position": "3"},
        {"uname": "丁", "dept": "005", "gross": "70", "rank_position": "4"},
    )
    card = build_card(
        spec,
        capability_id="fr008_payroll_ranking",
        title="员工工资清单与排名",
        columns=(
            CardColumn("uname", title="姓名"),
            CardColumn("dept", title="车间/小组"),
            CardColumn("gross", title="工资金额", column_type="money", unit="元"),
            CardColumn("rank_position", title="名次", column_type="quantity"),
        ),
        rows=rows,
        totals={"gross": Decimal("340")},
    )

    table = card["table"]
    assert table["group_by"] == "dept"
    assert table["groups_total"] == 2
    # flat rows stay sliced by group in groups[] order — the client never re-groups
    assert [entry["group"] for entry in table["groups"]] == ["001", "005"]
    first = table["groups"][0]
    assert first == {"group": "001", "row_count": 2, "total_rows": 3, "truncated": True}
    assert table["groups"][1]["truncated"] is False
    assert len(table["rows"]) == 3
    assert table["rows"][0] == ["甲", "001", "100", "1"]
    assert card["totals"] == [{"label": "工资金额", "unit": "元", "value": "340"}]


def test_grouped_card_drops_groups_beyond_the_group_cap() -> None:
    spec = CardTableSpec(
        kind="table",
        preview_max_rows=5,
        preview_max_groups=2,
        group_by="dept",
    )
    rows = tuple({"dept": str(100 + index), "gross": "1"} for index in range(4))
    card = build_card(
        spec,
        capability_id="fr008_payroll_ranking",
        title="员工工资清单与排名",
        columns=(CardColumn("dept", title="车间/小组"), CardColumn("gross", title="工资金额")),
        rows=rows,
        totals={},
    )

    table = card["table"]
    assert len(table["groups"]) == 2
    assert table["groups_total"] == 4
    assert table["truncated"] is True


def test_alert_marker_is_passed_through_as_semantics_only() -> None:
    spec = CardTableSpec(
        kind="table",
        preview_max_rows=10,
        alert_marker=CardAlertSpec(column="delivery_warning", equals="1"),
    )
    card = build_card(
        spec,
        capability_id="fr009_factory_order_overview",
        title="全厂订单进度总览",
        columns=(
            CardColumn("order_code", title="生产单号"),
            CardColumn("delivery_warning", title="交期预警"),
        ),
        rows=({"order_code": "DD001", "delivery_warning": "1"},),
        totals={},
    )

    assert card["table"]["alert_marker"] == {"column": "delivery_warning", "equals": "1"}


def test_unavailable_cells_render_as_null_and_incomplete_adds_a_note() -> None:
    spec = CardTableSpec(kind="table", preview_max_rows=10)
    card = build_card(
        spec,
        capability_id="fr005_order_progress",
        title="订单/款号进度",
        columns=(
            CardColumn("order_code", title="生产单号"),
            CardColumn("progress_ratio", title="进度", column_type="percent"),
        ),
        rows=({"order_code": "DD001", "progress_ratio": "unavailable"},),
        totals={},
        incomplete=True,
        incomplete_reason="pagination_total_drift",
        warnings=("分页拉取未完整：total_drift",),
    )

    assert card["table"]["rows"] == [["DD001", None]]
    notes = card["notes"]
    assert "分页拉取未完整：total_drift" in notes
    assert any("不完整" in note for note in notes)
