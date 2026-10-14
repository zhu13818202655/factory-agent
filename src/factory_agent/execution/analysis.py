import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sageql_core import ActionRejected, AnalysisPlan, RunContext, RunControl, Submission

from factory_agent.execution.bounded import run_sandbox_call
from factory_agent.execution.result_table import ResultColumnMeta, ResultTable
from factory_agent.ports.analysis_catalogue import (
    DATASETS,
    FACT_DATASETS,
    FIELD_TITLES,
    NUMERIC_FIELDS,
    SEMANTIC_VERSION,
    SENSITIVE_FIELDS,
)
from factory_agent.ports.analysis_data import LoadedDataset
from factory_agent.ports.analysis_host import AnalysisHostPort

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_JOINS = frozenset(
    {
        ("reported_production", "style_id", "styles", "style_id"),
        ("production_progress", "style_id", "styles", "style_id"),
        ("reported_production", "process_id", "processes", "process_id"),
        ("production_progress", "process_id", "processes", "process_id"),
        ("style_processes", "style_id", "styles", "style_id"),
        ("style_colors", "style_id", "styles", "style_id"),
        ("style_sizes", "style_id", "styles", "style_id"),
        ("style_processes", "process_id", "processes", "process_id"),
        ("bill_processes", "process_id", "processes", "process_id"),
        ("style_processes", "type_id", "production_types", "type_id"),
        ("bill_processes", "type_id", "production_types", "type_id"),
        ("styles", "type_id", "style_types", "type_id"),
    }
)


@dataclass(frozen=True, slots=True)
class CompiledAnalysis:
    sql: str
    params: tuple[Any, ...]
    columns: tuple[ResultColumnMeta, ...]
    scan_rows: int


class StructuredAnalysisCompiler:
    def __init__(self, host: AnalysisHostPort) -> None:
        self.host = host

    def compile(self, plan: AnalysisPlan) -> CompiledAnalysis:
        try:
            plan = AnalysisPlan.model_validate(plan.model_dump())
        except ValidationError:
            raise ActionRejected("invalid_action") from None
        loaded = tuple(self.host.get_dataset(reference) for reference in plan.dataset_refs)
        aliases = {item.reference: f"d{index}" for index, item in enumerate(loaded)}
        by_ref = {item.reference: item for item in loaded}
        operations = tuple(dict.fromkeys(item.query.dataset_id for item in loaded))
        primary = loaded[0]
        from_sql = f'"{primary.table_name}" AS d0'
        connected = {primary.reference}
        for join in plan.joins:
            if join.left_ref not in connected or join.right_ref in connected:
                raise ActionRejected("invalid_action")
            left, right = by_ref[join.left_ref], by_ref[join.right_ref]
            self._validate_join(left, right, join.left_keys, join.right_keys, join.cardinality)
            right_alias = aliases[join.right_ref]
            clauses = [
                f'{aliases[left.reference]}."{key}" = {right_alias}."{other}"'
                for key, other in zip(join.left_keys, join.right_keys)
            ]
            from_sql += f' JOIN "{right.table_name}" AS {right_alias} ON ' + " AND ".join(clauses)
            connected.add(right.reference)
        if connected != set(plan.dataset_refs):
            raise ActionRejected("invalid_action")

        def field_expression(name: str, *, allow_sensitive: bool = False) -> tuple[str, str]:
            prefix, separator, field = name.partition(".")
            if not separator:
                field = name
            if not _IDENTIFIER.fullmatch(field):
                raise ActionRejected("invalid_action")
            choices = [
                item
                for item in loaded
                if field in item.columns and (not separator or item.query.dataset_id == prefix)
            ]
            if len(choices) != 1:
                if not choices and any(
                    field in DATASETS[item.query.dataset_id].fields for item in loaded
                ):
                    raise ActionRejected("unavailable")
                raise ActionRejected("invalid_action")
            if field in SENSITIVE_FIELDS and not allow_sensitive:
                raise ActionRejected("denied")
            return f'{aliases[choices[0].reference]}."{field}"', field

        select: list[str] = []
        group: list[str] = []
        columns: list[ResultColumnMeta] = []
        output_names: set[str] = set()
        for name in plan.group_by:
            expression, field = field_expression(name)
            output = name.replace(".", "_")
            if output in output_names:
                raise ActionRejected("invalid_action")
            output_names.add(output)
            select.append(f'{expression} AS "{output}"')
            group.append(expression)
            columns.append(
                ResultColumnMeta(
                    output, None, None, operations, "text", title=FIELD_TITLES.get(field, field)
                )
            )
        if plan.time_bucket is not None:
            expression, field = field_expression(plan.time_bucket.field)
            if field != "business_date":
                raise ActionRejected("invalid_action")
            bucket = f"CAST(date_trunc('{plan.time_bucket.unit}', {expression}) AS DATE)"
            select.append(f'{bucket} AS "period"')
            group.append(bucket)
            output_names.add("period")
            columns.append(ResultColumnMeta("period", None, None, operations, "text", title="日期"))
        for metric in plan.metrics:
            expression, field = field_expression(metric.metric_id, allow_sensitive=True)
            self.host.authorize_metric(field)
            owner = next(
                item
                for item in loaded
                if field in item.columns
                and (
                    "." not in metric.metric_id
                    or item.query.dataset_id == metric.metric_id.split(".")[0]
                )
            )
            if field not in DATASETS[owner.query.dataset_id].metrics:
                raise ActionRejected("unavailable")
            output = field + "_" + metric.operation
            if output in output_names:
                raise ActionRejected("invalid_action")
            output_names.add(output)
            if metric.operation == "sum":
                if field not in NUMERIC_FIELDS:
                    raise ActionRejected("unavailable")
                calculation = f"COALESCE(SUM({expression}), CAST(0 AS DECIMAL(28,8)))"
            elif metric.operation == "count_distinct":
                if field in NUMERIC_FIELDS or field in SENSITIVE_FIELDS:
                    raise ActionRejected("denied")
                calculation = f"COUNT(DISTINCT NULLIF({expression}, ''))"
            else:
                if field != "configuration_id" or owner.query.dataset_id in FACT_DATASETS:
                    raise ActionRejected("unavailable")
                calculation = "COUNT(*)"
            select.append(f'{calculation} AS "{output}"')
            metric_name, metric_version, unit = _metric_metadata(field, metric.operation)
            columns.append(
                ResultColumnMeta(
                    output,
                    metric_name,
                    metric_version,
                    operations,
                    "money" if field == "payroll_amount" else "quantity",
                    unit,
                    FIELD_TITLES.get(output, FIELD_TITLES.get(field, field)),
                )
            )
        if not select:
            for field in primary.columns:
                if field in SENSITIVE_FIELDS:
                    continue
                output_names.add(field)
                select.append(f'd0."{field}" AS "{field}"')
                columns.append(
                    ResultColumnMeta(
                        field, None, None, operations, "text", title=FIELD_TITLES.get(field, field)
                    )
                )
        if not select:
            raise ActionRejected("unavailable")
        params: list[Any] = []
        conditions: list[str] = []
        for condition in plan.filters:
            expression, _field = field_expression(condition.field)
            if condition.operator == "in":
                placeholders = ", ".join("?" for _ in condition.values)
                conditions.append(f"{expression} IN ({placeholders})")
            else:
                symbol = {"eq": "=", "gte": ">=", "lt": "<"}[condition.operator]
                conditions.append(f"{expression} {symbol} ?")
            params.extend(condition.values)
        # Identifiers are resolved from validated handles; filter values are bound parameters.
        sql = "SELECT " + ", ".join(select) + " FROM " + from_sql  # nosec B608
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        if group:
            sql += " GROUP BY " + ", ".join(group)
        ordering: list[str] = []
        for item in plan.order_by:
            name = item.field.replace(".", "_")
            if name not in output_names:
                raise ActionRejected("invalid_action")
            ordering.append(f'"{name}" {item.direction.upper()}')
        if ordering:
            sql += " ORDER BY " + ", ".join(ordering)
        elif group:
            sql += " ORDER BY " + ", ".join(
                f'"{column.name}"' for column in columns if column.metric_name is None
            )
        sql += " LIMIT " + str(plan.limit + 1)
        return CompiledAnalysis(
            sql, tuple(params), tuple(columns), sum(len(item.rows) for item in loaded)
        )

    @staticmethod
    def _validate_join(
        left: LoadedDataset,
        right: LoadedDataset,
        left_keys: tuple[str, ...],
        right_keys: tuple[str, ...],
        cardinality: str,
    ) -> None:
        if left.query.dataset_id in FACT_DATASETS and right.query.dataset_id in FACT_DATASETS:
            raise ActionRejected("unavailable")
        if (
            len(left_keys) != 1
            or (left.query.dataset_id, left_keys[0], right.query.dataset_id, right_keys[0])
            not in _JOINS
        ):
            raise ActionRejected("unavailable")
        if any(key not in left.columns for key in left_keys) or any(
            key not in right.columns for key in right_keys
        ):
            raise ActionRejected("unavailable")
        left_values = [tuple(row.get(key) for key in left_keys) for row in left.rows]
        right_values = [tuple(row.get(key) for key in right_keys) for row in right.rows]
        if any(
            any(value in {None, ""} for value in keys) for keys in (*left_values, *right_values)
        ):
            raise ActionRejected("unavailable")
        if len(right_values) != len(set(right_values)):
            raise ActionRejected("unavailable")
        if cardinality == "one_to_one" and len(left_values) != len(set(left_values)):
            raise ActionRejected("unavailable")
        if not set(left_values) <= set(right_values):
            raise ActionRejected("unavailable")


def _metric_metadata(field: str, operation: str) -> tuple[str, str, str | None]:
    mappings = {
        "quantity": ("output_reported_qty", "customer-cl-v2", "件·工序"),
        "payroll_amount": ("output_reported_amount", "customer-cl-v2", "元"),
        "issued_quantity": ("progress_issued_qty", "customer-progress-v2", "件"),
        "received_quantity": ("progress_received_qty", "customer-progress-v2", "件"),
    }
    if operation == "sum" and field in mappings:
        return mappings[field]
    return (
        "configuration_" + field + "_" + operation,
        SEMANTIC_VERSION,
        "种" if operation == "count_distinct" else "条",
    )


class HostAnalysisExecutor:
    def __init__(self, host: AnalysisHostPort) -> None:
        self.host = host

    async def analyze(self, context: RunContext, plan: AnalysisPlan, control: RunControl) -> object:
        await self.host.validate(context, control)
        compiled = StructuredAnalysisCompiler(self.host).compile(plan)
        control.consume("processed_rows", compiled.scan_rows)
        names, values = await run_sandbox_call(
            self.host.sandbox,
            lambda: self.host.sandbox.execute_typed(compiled.sql, compiled.params),
            control,
        )
        if len(values) > plan.limit:
            return self.host.observation(
                state="incomplete", complete=False, reason_code="incomplete"
            )
        rows = tuple(dict(zip(names, row)) for row in values)
        encoded_size = len(json.dumps(rows, default=str, ensure_ascii=False).encode())
        control.consume("result_bytes", encoded_size)
        operations = tuple(
            dict.fromkeys(
                self.host.get_dataset(reference).query.dataset_id for reference in plan.dataset_refs
            )
        )
        totals: dict[str, Decimal] = {}
        for column in compiled.columns:
            if column.metric_name is None:
                continue
            if column.name.endswith("_count_distinct") and (plan.group_by or plan.time_bucket):
                continue
            totals[column.name] = sum((Decimal(str(row[column.name])) for row in rows), Decimal(0))
        warnings: list[str] = []
        if any(item.name == "quantity_sum" for item in compiled.columns):
            warnings.append("报工量单位为件·工序；本窗口无报工不表示床号不存在。")
        if "production_progress" in operations:
            warnings.append(
                "进度窗口按制单日期筛选；实收数量是查询时累计值，不是窗口内新增报工量。"
            )
        for loaded_ref in plan.dataset_refs:
            loaded = self.host.get_dataset(loaded_ref)
            if loaded.query.dataset_id in {"reported_production", "production_progress"}:
                start = loaded.descriptor.window_start
                end = loaded.descriptor.window_end
                if start is not None and end is not None:
                    warnings.append(
                        f"本结果 {loaded.query.dataset_id} 的实际输入窗口："
                        f"[{start.isoformat()}, {end.isoformat()})；"
                        f"时区 {loaded.descriptor.timezone}。"
                    )
            for condition in loaded.query.filters:
                if condition.field not in {"style_id", "configuration_id"}:
                    warnings.append(
                        f"数据条件 {condition.field} {condition.operator}："
                        + "、".join(str(value) for value in condition.values)
                        + "。"
                    )
        for condition in plan.filters:
            if condition.field.rsplit(".", 1)[-1] not in {"style_id", "configuration_id"}:
                warnings.append(
                    f"计算条件 {condition.field} {condition.operator}："
                    + "、".join(str(value) for value in condition.values)
                    + "。"
                )
        configuration_datasets = [
            self.host.get_dataset(reference)
            for reference in plan.dataset_refs
            if self.host.get_dataset(reference).query.dataset_id
            in {"style_processes", "bill_processes", "style_sizes", "style_colors"}
        ]
        if configuration_datasets:
            warnings.append("配置种类按非空原始编码去重；配置记录数量单独计算。")
            active_filtered = any(
                condition.field == "active"
                for loaded in configuration_datasets
                for condition in loaded.query.filters
            ) or any(condition.field == "active" for condition in plan.filters)
            warnings.append(
                "配置使用状态已按明确条件筛选。"
                if active_filtered
                else "当前配置结果未按使用状态筛选。"
            )
        result = ResultTable(
            "l2_analysis",
            compiled.columns,
            rows,
            totals,
            operations,
            warnings=tuple(warnings),
        )
        reference = self.host.register_result(result)
        return self.host.observation(
            state="analyzed",
            result_refs=(reference,),
            row_count=len(rows),
            complete=True,
            source="interaction",
        )

    async def validate_submission(
        self, context: RunContext, submission: Submission, control: RunControl
    ) -> bool:
        await self.host.validate(context, control)
        if submission.kind == "result":
            return bool(submission.result_refs) and all(
                reference in self.host.results for reference in submission.result_refs
            )
        if submission.kind == "knowledge":
            return bool(submission.knowledge_refs) and all(
                reference in self.host.knowledge for reference in submission.knowledge_refs
            )
        return submission.kind in {"clarify", "unavailable"}
