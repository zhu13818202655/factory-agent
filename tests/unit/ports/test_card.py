"""Card builder tests: numbers only from the result table, no fabrication."""

from decimal import Decimal

from factory_agent.ports.card import CardAlertSpec, CardColumn, CardTableSpec, build_card
from tests.support.payload import as_dict, as_list

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
    metrics = as_list(card["metrics"])
    assert metrics[0] == {"label": "计件工资合计", "unit": "元", "value": "21.65"}
    # totals take precedence over the row value; a formatted string cell is fine
    assert metrics[2] == {"label": "日均工资", "unit": "元", "value": "0.35"}
    assert "table" not in card


def test_money_and_percent_cells_round_to_two_decimals() -> None:
    """日均工资等计算均值在卡片上最多保留两位小数（与答案文本口径一致）。"""
    card = build_card(
        _kpi_spec(),
        capability_id="fr002_personal_wage_summary",
        title="个人工资汇总",
        columns=_WAGE_COLUMNS,
        rows=(
            {
                "gross_total": Decimal("16167.95"),
                "piece_count": Decimal("7650"),
                "daily_avg": Decimal("538.9316666666666667"),
            },
        ),
        totals={},
    )

    metrics = as_list(card["metrics"])
    assert metrics[0] == {"label": "计件工资合计", "unit": "元", "value": "16167.95"}
    assert metrics[2] == {"label": "日均工资", "unit": "元", "value": "538.93"}

    # 字符串数值同样按列类型四舍五入；普通整数不带多余小数位。
    table_card = build_card(
        CardTableSpec(kind="table", preview_max_rows=10),
        capability_id="fr005_order_progress",
        title="订单/款号进度",
        columns=(
            CardColumn("order_code", title="生产单号"),
            CardColumn("progress_ratio", title="包完工率", column_type="percent"),
        ),
        rows=(
            {"order_code": "DD001", "progress_ratio": "85.7142857142857"},
            {"order_code": "DD002", "progress_ratio": "100"},
        ),
        totals={},
    )
    table = as_dict(table_card["table"])
    assert table["rows"] == [["DD001", "85.71"], ["DD002", "100"]]


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

    metrics = as_list(card["metrics"])
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

    table = as_dict(card["table"])
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

    table = as_dict(card["table"])
    assert table["group_by"] == "dept"
    assert table["groups_total"] == 2
    # flat rows stay sliced by group in groups[] order — the client never re-groups
    assert [entry["group"] for entry in table["groups"]] == ["001", "005"]
    first = table["groups"][0]
    assert first == {
        "group": "001",
        "name": "001",
        "row_count": 2,
        "total_rows": 3,
        "truncated": True,
    }
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

    table = as_dict(card["table"])
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

    assert as_dict(card["table"])["alert_marker"] == {"column": "delivery_warning", "equals": "1"}


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

    assert as_dict(card["table"])["rows"] == [["DD001", None]]
    notes = as_list(card["notes"])
    assert "分页拉取未完整：total_drift" in notes
    assert any("不完整" in note for note in notes)


# ----------------------------------------------------------------------
# 2026-09-16 卡片协议增量：分页三字段 / groups[].name / chart / actions / aux KPI
# ----------------------------------------------------------------------


def test_table_card_emits_the_pagination_triplet_in_preview_mode() -> None:
    """page/page_size/has_more 三字段一起下发，has_more 派生自 truncated."""
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

    table = as_dict(card["table"])
    assert table["page"] == 1
    assert table["page_size"] == 2
    assert table["has_more"] is True


def test_table_card_without_truncation_reports_has_more_false() -> None:
    spec = CardTableSpec(kind="table", preview_max_rows=10)
    card = build_card(
        spec,
        capability_id="fr003_personal_wage_detail",
        title="个人工资明细",
        columns=(CardColumn("rq", title="日期", column_type="date"),),
        rows=({"rq": "2026-09-01"},),
        totals={},
    )
    table = as_dict(card["table"])
    assert table["truncated"] is False
    assert table["has_more"] is False


def test_group_name_is_taken_from_the_declared_column() -> None:
    spec = CardTableSpec(
        kind="ranking",
        preview_max_rows=2,
        preview_max_groups=50,
        group_by="dept",
        group_name_from="dept_name",
        rank_column="rank_position",
    )
    rows = (
        {"uname": "甲", "dept": "001", "dept_name": "缝纫一组", "rank_position": "1"},
        {"uname": "乙", "dept": "005", "dept_name": "缝纫五组", "rank_position": "2"},
    )
    card = build_card(
        spec,
        capability_id="fr008_payroll_ranking",
        title="员工工资清单与排名",
        columns=(
            CardColumn("uname", title="姓名"),
            CardColumn("dept", title="车间/小组"),
            CardColumn("dept_name", title="小组名称"),
            CardColumn("rank_position", title="名次", column_type="quantity"),
        ),
        rows=rows,
        totals={},
    )

    groups = as_list(as_dict(card["table"])["groups"])
    assert groups[0] == {
        "group": "001",
        "name": "缝纫一组",
        "row_count": 1,
        "total_rows": 1,
        "truncated": False,
    }
    assert groups[1]["name"] == "缝纫五组"


def test_card_actions_are_declared_as_card_level_drill_entries() -> None:
    from factory_agent.ports.card import CardAction

    spec = CardTableSpec(
        kind="table",
        preview_max_rows=10,
        actions=(
            CardAction(
                label="查看该成员的工资",
                capability_id="fr012_employee_payroll",
                bind_from={"employee_uid": "uid"},
            ),
        ),
    )
    card = build_card(
        spec,
        capability_id="fr008_payroll_ranking",
        title="员工工资清单与排名",
        columns=(CardColumn("uid", title="工号"),),
        rows=({"uid": "01001"},),
        totals={},
    )

    assert card["actions"] == [
        {
            "type": "drill",
            "label": "查看该成员的工资",
            "capability_id": "fr012_employee_payroll",
            "bind_from": {"employee_uid": "uid"},
        }
    ]


def test_aux_metrics_render_values_and_explicit_unavailable() -> None:
    card = build_card(
        CardTableSpec(kind="table", preview_max_rows=10),
        capability_id="fr011_factory_payroll_stats",
        title="全厂工资统计",
        columns=(CardColumn("dept_name", title="车间/小组"),),
        rows=({"dept_name": "一车间"},),
        totals={},
        aux_metrics=(
            ("工资总额", "元", "23.75"),
            ("人均工资", "元", "unavailable"),
            ("在册人数", "人", None),
        ),
    )

    metrics = as_list(card["metrics"])
    assert metrics[0] == {"label": "工资总额", "unit": "元", "value": "23.75"}
    assert metrics[1] == {"label": "人均工资", "unit": "元", "unavailable": True}
    assert metrics[2] == {"label": "在册人数", "unit": "人", "unavailable": True}


def test_chart_payload_keeps_day_granularity_within_the_point_cap() -> None:
    from factory_agent.ports.card import build_chart_payload

    chart = build_chart_payload(
        chart_type="bar",
        unit="件",
        labels=["2026-09-01", "2026-09-02", "2026-09-03"],
        values=["10", "20", "9.5"],
    )

    assert chart.type == "bar"
    assert chart.granularity == "day"
    assert chart.points == (
        ("09-01", "10"),
        ("09-02", "20"),
        ("09-03", "9.5"),
    )


def test_chart_payload_downsamples_to_weeks_beyond_the_point_cap() -> None:
    from datetime import date, timedelta

    from factory_agent.ports.card import MAX_CHART_DAY_POINTS, build_chart_payload

    start = date(2026, 8, 3)  # Monday
    labels = [(start + timedelta(days=day)).isoformat() for day in range(MAX_CHART_DAY_POINTS + 5)]
    values = ["1"] * len(labels)
    chart = build_chart_payload(chart_type="bar", unit="件", labels=labels, values=values)

    assert chart.granularity == "week"
    # 68 天横跨 10 个自然周（周一起算），每周桶内求和 = 7（末周除外）。
    assert len(chart.points) == 10
    assert chart.points[0] == ("08-03", "7")


def test_chart_payload_ignores_unparseable_labels() -> None:
    from factory_agent.ports.card import build_chart_payload

    chart = build_chart_payload(
        chart_type="bar",
        unit="件",
        labels=["2026-09-01", "not-a-date", ""],
        values=["10", "20", "30"],
    )
    assert chart.points == (("09-01", "10"),)


def test_build_card_embeds_the_chart_payload() -> None:
    from factory_agent.ports.card import build_chart_payload

    chart = build_chart_payload(chart_type="bar", unit="件", labels=["2026-09-01"], values=["12"])
    card = build_card(
        CardTableSpec(kind="ranking", preview_max_rows=10, rank_column="rank_position"),
        capability_id="fr013_factory_output_dashboard",
        title="全厂产量总览",
        columns=(CardColumn("dept_name", title="车间/小组"),),
        rows=({"dept_name": "一车间"},),
        totals={},
        chart=chart,
    )

    assert card["chart"] == {
        "type": "bar",
        "unit": "件",
        "granularity": "day",
        "points": [{"label": "09-01", "value": "12"}],
    }
