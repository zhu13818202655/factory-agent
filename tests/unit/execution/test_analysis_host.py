import asyncio
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from sageql_core import (
    ActionRejected,
    AnalysisPlan,
    ApprovedObservation,
    Filter,
    Join,
    Metric,
    Query,
    RunContext,
    RunControl,
    Sort,
    Submission,
    TimeBucket,
)

from factory_agent.application.l2.provider import (
    AnalysisHost,
    HostDatasetProvider,
    HostObservationPolicy,
)
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    IntentSlots,
    Role,
    ScopeVersion,
    TenantContext,
    TenantId,
    TimeRange,
    UserId,
)
from factory_agent.execution.analysis import HostAnalysisExecutor
from factory_agent.execution.sandbox_runtime import InteractionSandbox
from factory_agent.ports.analysis import AnalysisRequest
from factory_agent.ports.analysis_data import AnalysisValue, FactBatch, FactQuery
from factory_agent.ports.dictionary import DictionaryBatch, DictionaryValue

NOW = datetime(2026, 10, 13, 0, tzinfo=UTC)
WINDOW = TimeRange(
    datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
    datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
)


async def active() -> bool:
    return True


@pytest.mark.asyncio
async def test_optional_invalid_order_does_not_hide_valid_process_dictionary() -> None:
    host = host_for(
        dictionaries=Dictionaries(
            {
                "processes": (
                    {"process_id": "P1", "name": "合成工序", "sort_order": "", "deleted": False},
                )
            }
        ),
        slots=IntentSlots(),
    )
    control = control_for(host)
    try:
        loaded = await ensure(
            host, Query(dataset_id="processes", columns=("process_id", "name")), control
        )
        assert loaded.complete
        descriptor = loaded.descriptors[0]
        assert not next(
            field for field in descriptor.fields if field.name == "sort_order"
        ).validated
        assert (
            next(field for field in descriptor.fields if field.name == "deleted").data_type
            == "boolean"
        )
        assert host.get_dataset(loaded.dataset_refs[0]).rows[0]["deleted"] is False
        invalid = await ensure(
            host, Query(dataset_id="processes", columns=("sort_order",)), control
        )
        assert invalid.state == "unavailable"
        assert not invalid.complete
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_configuration_order_preserves_integer_sorting_in_sandbox() -> None:
    host = host_for(
        dictionaries=Dictionaries(
            {
                "processes": (
                    {"process_id": "P1", "name": "甲", "sort_order": "1"},
                    {"process_id": "P2", "name": "丙", "sort_order": "10"},
                    {"process_id": "P3", "name": "乙", "sort_order": "2"},
                )
            }
        ),
        slots=IntentSlots(),
    )
    control = control_for(host)
    try:
        loaded = await ensure(host, Query(dataset_id="processes"), control)
        observed = cast(
            ApprovedObservation,
            await HostAnalysisExecutor(host).analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=loaded.dataset_refs,
                    group_by=("name", "sort_order"),
                    metrics=(Metric(metric_id="process_id", operation="count_distinct"),),
                    order_by=(Sort(field="sort_order", direction="asc"),),
                ),
                control,
            ),
        )
        table = host.get_result(observed.result_refs[0])
        assert [row["sort_order"] for row in table.rows] == [1, 2, 10]
        assert [row["name"] for row in table.rows] == ["甲", "乙", "丙"]
    finally:
        host.close()
        host.sandbox.close()


class Facts:
    def __init__(self, rows: tuple[Mapping[str, AnalysisValue], ...] = ()) -> None:
        self.rows = rows
        self.calls: list[FactQuery] = []

    async def fetch(self, query: FactQuery) -> FactBatch:
        self.calls.append(query)
        await asyncio.sleep(0)
        rows = tuple(
            row
            for row in self.rows
            if query.filters.bed_codes is None
            or "bed_code" not in row
            or row.get("bed_code") in query.filters.bed_codes
        )
        return FactBatch(query.dataset_id, rows, True, generated_at=NOW, cache_source="cache")


class Dictionaries:
    def __init__(
        self, datasets: dict[str, tuple[Mapping[str, DictionaryValue], ...]] | None = None
    ) -> None:
        self.datasets = datasets or {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def fetch(
        self, dataset_id: str, scope: DataScope, entity_ids: tuple[str, ...] = ()
    ) -> DictionaryBatch:
        self.calls.append((dataset_id, entity_ids))
        rows = self.datasets.get(dataset_id, ())
        return DictionaryBatch(dataset_id, rows, len(rows), True, NOW, entity_ids, "cache")


def host_for(
    facts: Facts | None = None,
    dictionaries: Dictionaries | None = None,
    role: Role = Role.OWNER,
    slots: IntentSlots | None = None,
) -> AnalysisHost:
    request = AnalysisRequest(
        "interaction-a",
        "合成查询",
        TenantContext(TenantId("tenant-a"), UserId("worker-a"), EmployeeId("worker-a"), role, NOW),
        DataScope(
            TenantId("tenant-a"),
            frozenset({EmployeeId("worker-a")}),
            frozenset({DeptId("dept-a")}),
            NOW,
            ScopeVersion("scope-v1"),
            role is Role.OWNER,
        ),
        slots or IntentSlots(bed_codes=("1555",)),
        WINDOW,
        time.monotonic() + 60,
        lambda: False,
        active,
    )
    return AnalysisHost(
        request, facts or Facts(), dictionaries or Dictionaries(), InteractionSandbox()
    )


def control_for(host: AnalysisHost) -> RunControl:
    return RunControl(host.request.deadline)


async def ensure(host: AnalysisHost, query: Query, control: RunControl) -> ApprovedObservation:
    return cast(
        ApprovedObservation,
        await HostDatasetProvider(host).ensure(RunContext(host), query, control),
    )


@pytest.mark.asyncio
async def test_one_bed_handle_is_reused_for_grouping_and_trend_with_zero_source_requests() -> None:
    facts = Facts(
        (
            {
                "plan_code": "P1",
                "bed_code": "1555",
                "business_date": "2026-09-02",
                "quantity": "2",
                "process_id": "A",
            },
            {
                "plan_code": "P1",
                "bed_code": "1555",
                "business_date": "2026-09-03",
                "quantity": "3",
                "process_id": "B",
            },
            {
                "plan_code": "P2",
                "bed_code": "01555",
                "business_date": "2026-09-03",
                "quantity": "999",
                "process_id": "B",
            },
        )
    )
    host = host_for(facts)
    control = control_for(host)
    query = Query(
        dataset_id="reported_production",
        columns=("plan_code", "bed_code", "business_date", "quantity", "process_id"),
    )
    try:
        first, second = await asyncio.gather(
            ensure(host, query, control), ensure(host, query, control)
        )
        assert first.dataset_refs == second.dataset_refs
        assert second.state == "interaction_reuse"
        assert len(facts.calls) == 1
        reordered = await ensure(
            host,
            Query(dataset_id="reported_production", columns=("quantity", "plan_code")),
            control,
        )
        assert reordered.dataset_refs == first.dataset_refs
        assert reordered.state == "interaction_reuse"
        assert len(facts.calls) == 1
        assert facts.calls[0].filters.bed_codes == frozenset({"1555"})
        assert first.source == "cache"
        assert control.used("mes_requests") == 0
        assert "worker-a" not in first.model_dump_json()
        executor = HostAnalysisExecutor(host)
        grouped = cast(
            ApprovedObservation,
            await executor.analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=first.dataset_refs,
                    group_by=("plan_code",),
                    metrics=(Metric(metric_id="quantity"),),
                ),
                control,
            ),
        )
        trend = cast(
            ApprovedObservation,
            await executor.analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=first.dataset_refs,
                    metrics=(Metric(metric_id="quantity"),),
                    time_bucket=TimeBucket(field="business_date", unit="day"),
                ),
                control,
            ),
        )
        assert host.get_result(grouped.result_refs[0]).rows == (
            {"plan_code": "P1", "quantity_sum": Decimal("5")},
        )
        assert [row["quantity_sum"] for row in host.get_result(trend.result_refs[0]).rows] == [
            Decimal("2"),
            Decimal("3"),
        ]
        assert len(facts.calls) == 1
        assert await executor.validate_submission(
            RunContext(host), Submission(kind="result", result_refs=grouped.result_refs), control
        )
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_complete_empty_data_is_reused_and_has_a_proven_zero_contribution() -> None:
    host = host_for()
    control = control_for(host)
    try:
        query = Query(dataset_id="reported_production", columns=("quantity",))
        first = await ensure(host, query, control)
        second = await ensure(host, query, control)
        assert first.state == "empty"
        assert first.dataset_refs == second.dataset_refs
        analyzed = cast(
            ApprovedObservation,
            await HostAnalysisExecutor(host).analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=first.dataset_refs, metrics=(Metric(metric_id="quantity"),)
                ),
                control,
            ),
        )
        assert host.get_result(analyzed.result_refs[0]).totals == {"quantity_sum": Decimal(0)}
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.parametrize(
    "bad_row",
    [
        {"quantity": "2"},
        {"bed_code": "1555", "quantity": "not-a-number"},
        {"bed_code": "1555", "quantity": None},
        {"bed_code": "1555", "quantity": "0.123456789"},
        {"bed_code": "1555", "quantity": "100000000000000000000"},
    ],
)
@pytest.mark.asyncio
async def test_missing_and_invalid_contributions_never_create_result_handles(
    bad_row: Mapping[str, AnalysisValue],
) -> None:
    host = host_for(Facts((bad_row,)))
    control = control_for(host)
    try:
        loaded = await ensure(
            host, Query(dataset_id="reported_production", columns=("quantity", "bed_code")), control
        )
        assert loaded.state == "unavailable"
        assert loaded.dataset_refs == ()
        assert host.results == {}
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_dictionary_join_validates_stable_keys_and_counts_unreported_configuration() -> None:
    directories = Dictionaries(
        {
            "style_processes": (
                {"configuration_id": "C1", "style_id": "S1", "process_id": "P1", "type_id": "T1"},
                {"configuration_id": "C2", "style_id": "S1", "process_id": "P2", "type_id": "T1"},
                {"configuration_id": "C3", "style_id": "S1", "process_id": "P2", "type_id": "T1"},
            ),
            "production_types": ({"type_id": "T1", "name": "合成生产类型"},),
        }
    )
    host = host_for(dictionaries=directories, role=Role.EMPLOYEE, slots=IntentSlots())
    control = control_for(host)
    try:
        left = await ensure(host, Query(dataset_id="style_processes"), control)
        right = await ensure(host, Query(dataset_id="production_types"), control)
        outcome = cast(
            ApprovedObservation,
            await HostAnalysisExecutor(host).analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=(*left.dataset_refs, *right.dataset_refs),
                    group_by=("name",),
                    metrics=(
                        Metric(metric_id="process_id", operation="count_distinct"),
                        Metric(metric_id="configuration_id", operation="count"),
                    ),
                    joins=(
                        Join(
                            left_ref=left.dataset_refs[0],
                            right_ref=right.dataset_refs[0],
                            left_keys=("type_id",),
                            right_keys=("type_id",),
                            cardinality="many_to_one",
                        ),
                    ),
                ),
                control,
            ),
        )
        assert host.get_result(outcome.result_refs[0]).rows == (
            {"name": "合成生产类型", "process_id_count_distinct": 2, "configuration_id_count": 3},
        )
        assert len(directories.calls) == 2
        assert cast(Facts, host.fact_source).calls == []
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_unknown_refs_sql_text_scope_broadening_and_employee_progress_are_rejected() -> None:
    facts = Facts()
    host = host_for(facts, role=Role.EMPLOYEE)
    control = control_for(host)
    provider = HostDatasetProvider(host)
    try:
        with pytest.raises(ActionRejected):
            await provider.inspect(RunContext(host), "dataset:foreign", control)
        with pytest.raises(ActionRejected):
            await ensure(host, Query(dataset_id="production_progress"), control)
        with pytest.raises(ActionRejected):
            await ensure(
                host,
                Query(
                    dataset_id="reported_production",
                    window_start=WINDOW.start,
                    window_end=datetime(2027, 1, 1, tzinfo=UTC),
                ),
                control,
            )
        with pytest.raises(ActionRejected):
            await ensure(
                host,
                Query(
                    dataset_id="reported_production",
                    filters=(Filter(field="bed_code", values=("01555",)),),
                ),
                control,
            )
        with pytest.raises(ActionRejected):
            await ensure(
                host,
                Query(dataset_id="reported_production", columns=("read_csv('/secret')",)),
                control,
            )
        assert facts.calls == []
        assert not await HostAnalysisExecutor(host).validate_submission(
            RunContext(host), Submission(kind="result", result_refs=("result:foreign",)), control
        )
        with pytest.raises(ActionRejected):
            await HostObservationPolicy(host).approve(
                RunContext(host), "ensure_dataset", ApprovedObservation(state="available")
            )
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_bill_configuration_requires_progress_and_closed_handles_die() -> None:
    directories = Dictionaries()
    host = host_for(dictionaries=directories)
    control = control_for(host)
    try:
        with pytest.raises(ActionRejected):
            await ensure(
                host,
                Query(
                    dataset_id="bill_processes",
                    filters=(Filter(field="bill_code", values=("USER-SUPPLIED-BILL",)),),
                ),
                control,
            )
        assert directories.calls == []
        host.request = replace(host.request, validate_active=lambda: asyncio.sleep(0, result=False))
        with pytest.raises(ActionRejected):
            await ensure(host, Query(dataset_id="styles"), control)
        host.close()
        assert host.datasets == {}
        with pytest.raises(ActionRejected):
            host.get_dataset("dataset:old")
    finally:
        host.sandbox.close()


@pytest.mark.parametrize(
    "types",
    [
        ({"type_id": "T1", "name": "甲"}, {"type_id": "T1", "name": "乙"}),
        ({"type_id": "T2", "name": "甲"},),
        ({"type_id": "", "name": "甲"},),
    ],
)
@pytest.mark.asyncio
async def test_join_rejects_invalid_keys(types: tuple[Mapping[str, DictionaryValue], ...]) -> None:
    host = host_for(
        dictionaries=Dictionaries(
            {
                "style_processes": (
                    {"configuration_id": "C1", "process_id": "P1", "type_id": "T1"},
                ),
                "production_types": types,
            }
        ),
        slots=IntentSlots(),
    )
    control = control_for(host)
    try:
        left = await ensure(host, Query(dataset_id="style_processes"), control)
        right = await ensure(host, Query(dataset_id="production_types"), control)
        with pytest.raises(ActionRejected):
            await HostAnalysisExecutor(host).analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=(*left.dataset_refs, *right.dataset_refs),
                    metrics=(Metric(metric_id="configuration_id", operation="count"),),
                    joins=(
                        Join(
                            left_ref=left.dataset_refs[0],
                            right_ref=right.dataset_refs[0],
                            left_keys=("type_id",),
                            right_keys=("type_id",),
                            cardinality="many_to_one",
                        ),
                    ),
                ),
                control,
            )
        assert host.results == {}
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_distinct_totals_and_active_configuration_use_explicit_semantics() -> None:
    rows: tuple[Mapping[str, DictionaryValue], ...] = (
        {"configuration_id": "C1", "style_id": "S1", "process_id": "P1", "active": True},
        {"configuration_id": "C2", "style_id": "S2", "process_id": "P1", "active": False},
        {"configuration_id": "C3", "style_id": "S2", "process_id": "", "active": True},
    )
    host = host_for(dictionaries=Dictionaries({"style_processes": rows}), slots=IntentSlots())
    control = control_for(host)
    try:
        loaded = await ensure(host, Query(dataset_id="style_processes"), control)
        executor = HostAnalysisExecutor(host)
        groups = cast(
            ApprovedObservation,
            await executor.analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=loaded.dataset_refs,
                    group_by=("style_id",),
                    metrics=(Metric(metric_id="process_id", operation="count_distinct"),),
                ),
                control,
            ),
        )
        assert host.get_result(groups.result_refs[0]).totals == {}
        union = cast(
            ApprovedObservation,
            await executor.analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=loaded.dataset_refs,
                    metrics=(Metric(metric_id="process_id", operation="count_distinct"),),
                ),
                control,
            ),
        )
        assert host.get_result(union.result_refs[0]).totals == {
            "process_id_count_distinct": Decimal(1)
        }
        active_only = await ensure(
            host,
            Query(dataset_id="style_processes", filters=(Filter(field="active", values=(True,)),)),
            control,
        )
        active_result = cast(
            ApprovedObservation,
            await executor.analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=active_only.dataset_refs,
                    metrics=(Metric(metric_id="configuration_id", operation="count"),),
                ),
                control,
            ),
        )
        assert host.get_result(active_result.result_refs[0]).totals == {
            "configuration_id_count": Decimal(2)
        }
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_form_entity_ids_are_resolved_inside_host_and_never_exposed_to_model() -> None:
    directories = Dictionaries(
        {
            "styles": (
                {"style_id": "INTERNAL-A", "style_code": "USER-STYLE"},
                {"style_id": "INTERNAL-B", "style_code": "OTHER-STYLE"},
            ),
            "style_sizes": ({"configuration_id": "C1", "style_id": "INTERNAL-A", "size": "XXL"},),
        }
    )
    host = host_for(dictionaries=directories, slots=IntentSlots(style_codes=("USER-STYLE",)))
    control = control_for(host)
    try:
        styles = await ensure(host, Query(dataset_id="styles"), control)
        assert "INTERNAL-A" not in styles.model_dump_json()
        assert styles.row_count == 1
        sizes = await ensure(host, Query(dataset_id="style_sizes"), control)
        assert sizes.row_count == 1
        assert directories.calls == [("styles", ()), ("style_sizes", ("INTERNAL-A",))]
        with pytest.raises(ActionRejected):
            await ensure(
                host,
                Query(
                    dataset_id="style_colors",
                    filters=(Filter(field="style_id", values=("INTERNAL-B",)),),
                ),
                control,
            )
        assert len(directories.calls) == 2
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_bed_configuration_links_use_authorized_progress_and_hidden_entity_ids() -> None:
    facts = Facts(
        (
            {
                "bed_code": "1555",
                "style_id": "INTERNAL-A",
                "bill_code": "BILL-A",
                "business_date": "2026-09-02",
            },
            {
                "bed_code": "01555",
                "style_id": "INTERNAL-B",
                "bill_code": "BILL-B",
                "business_date": "2026-09-02",
            },
        )
    )
    directories = Dictionaries(
        {
            "styles": (
                {"style_id": "INTERNAL-A", "style_code": "VISIBLE-A"},
                {"style_id": "INTERNAL-B", "style_code": "VISIBLE-B"},
            ),
            "style_sizes": (
                {"configuration_id": "SZ-A", "style_id": "INTERNAL-A", "size": "XXL"},
                {"configuration_id": "SZ-B", "style_id": "INTERNAL-B", "size": "2XL"},
            ),
            "style_processes": (
                {"configuration_id": "P-A", "style_id": "INTERNAL-A", "process_id": "PROC-A"},
                {"configuration_id": "P-B", "style_id": "INTERNAL-B", "process_id": "PROC-B"},
            ),
            "bill_processes": (
                {"configuration_id": "BC-A", "bill_code": "BILL-A", "process_id": "PROC-A"},
            ),
        }
    )
    host = host_for(facts, directories)
    control = control_for(host)
    try:
        progress = await ensure(
            host,
            Query(
                dataset_id="production_progress",
                columns=("style_id", "bill_code", "business_date", "bed_code"),
            ),
            control,
        )
        styles = await ensure(host, Query(dataset_id="styles"), control)
        sizes = await ensure(host, Query(dataset_id="style_sizes"), control)
        configured = await ensure(host, Query(dataset_id="style_processes"), control)
        bills = await ensure(host, Query(dataset_id="bill_processes"), control)
        assert all(
            observation.row_count == 1
            for observation in (progress, styles, sizes, configured, bills)
        )
        assert directories.calls == [
            ("styles", ()),
            ("style_sizes", ("INTERNAL-A",)),
            ("style_processes", ()),
            ("bill_processes", ("BILL-A",)),
        ]
        for observation in (progress, styles, sizes, configured, bills):
            assert "INTERNAL-A" not in observation.model_dump_json()
            assert "INTERNAL-B" not in observation.model_dump_json()
            assert "BILL-A" not in observation.model_dump_json()
            assert "PROC-B" not in observation.model_dump_json()
        assert host.get_dataset(sizes.dataset_refs[0]).rows == (
            {"configuration_id": "SZ-A", "style_id": "INTERNAL-A", "size": "XXL"},
        )
        assert host.get_dataset(configured.dataset_refs[0]).rows == (
            {"configuration_id": "P-A", "style_id": "INTERNAL-A", "process_id": "PROC-A"},
        )
        assert len(facts.calls) == 1
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_progress_metrics_use_piece_unit_and_explicit_cumulative_semantics() -> None:
    host = host_for(
        Facts(
            (
                {
                    "bed_code": "1555",
                    "business_date": "2026-09-02",
                    "issued_quantity": "10",
                    "received_quantity": "8",
                },
            )
        )
    )
    control = control_for(host)
    try:
        loaded = await ensure(
            host,
            Query(
                dataset_id="production_progress", columns=("issued_quantity", "received_quantity")
            ),
            control,
        )
        analyzed = cast(
            ApprovedObservation,
            await HostAnalysisExecutor(host).analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=loaded.dataset_refs,
                    metrics=(
                        Metric(metric_id="received_quantity"),
                        Metric(metric_id="issued_quantity"),
                    ),
                ),
                control,
            ),
        )
        result = host.get_result(analyzed.result_refs[0])
        assert result.totals == {
            "received_quantity_sum": Decimal(8),
            "issued_quantity_sum": Decimal(10),
        }
        assert {column.unit for column in result.columns} == {"件"}
        assert any("累计" in warning and "不是窗口内新增" in warning for warning in result.warnings)
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_narrow_view_and_calculation_filter_echo_actual_range_without_refetch() -> None:
    facts = Facts(
        (
            {"bed_code": "1555", "business_date": "2026-09-02", "quantity": "2", "process_id": "A"},
            {"bed_code": "1555", "business_date": "2026-09-03", "quantity": "3", "process_id": "B"},
            {"bed_code": "1555", "business_date": "2026-09-04", "quantity": "5", "process_id": "A"},
        )
    )
    host = host_for(facts)
    control = control_for(host)
    start = datetime.fromisoformat("2026-09-02T00:00:00+08:00")
    end = datetime.fromisoformat("2026-09-04T00:00:00+08:00")
    try:
        await ensure(
            host,
            Query(
                dataset_id="reported_production",
                columns=("quantity", "business_date", "process_id"),
            ),
            control,
        )
        narrowed = await ensure(
            host,
            Query(
                dataset_id="reported_production",
                columns=("quantity",),
                window_start=start,
                window_end=end,
            ),
            control,
        )
        assert narrowed.state == "interaction_reuse"
        analyzed = cast(
            ApprovedObservation,
            await HostAnalysisExecutor(host).analyze(
                RunContext(host),
                AnalysisPlan(
                    dataset_refs=narrowed.dataset_refs,
                    metrics=(Metric(metric_id="quantity"),),
                    filters=(Filter(field="process_id", values=("B",)),),
                ),
                control,
            ),
        )
        result = host.get_result(analyzed.result_refs[0])
        assert result.totals == {"quantity_sum": Decimal(3)}
        assert any(
            start.isoformat() in warning and end.isoformat() in warning
            for warning in result.warnings
        )
        assert any("计算条件 process_id eq：B" in warning for warning in result.warnings)
        assert len(facts.calls) == 1
    finally:
        host.close()
        host.sandbox.close()


@pytest.mark.asyncio
async def test_fact_missing_business_date_is_unavailable_even_when_only_quantity_requested() -> (
    None
):
    host = host_for(Facts(({"bed_code": "1555", "quantity": "2"},)))
    control = control_for(host)
    try:
        unavailable = await ensure(
            host, Query(dataset_id="reported_production", columns=("quantity",)), control
        )
        assert unavailable.state == "unavailable"
        assert unavailable.complete is False
        assert "business_date" in unavailable.missing_fields
        assert unavailable.dataset_refs == ()
        assert host.datasets == {}
    finally:
        host.close()
        host.sandbox.close()
