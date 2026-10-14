import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sageql_core import (
    ActionRejected,
    ApprovedObservation,
    DatasetDescriptor,
    KnowledgeSnippet,
    Query,
    RunContext,
    RunControl,
    SemanticParameter,
)

from factory_agent.application.filters import FilterNarrower
from factory_agent.application.permission_matrix import Capability, authorize_capability
from factory_agent.domain import NarrowedFilters, Role, TimeRange, local_calendar_dates
from factory_agent.observability.local_trace import trace_local_operation
from factory_agent.ports.analysis import AnalysisRequest
from factory_agent.ports.analysis_catalogue import (
    BOOLEAN_FIELDS,
    DATASETS,
    DICTIONARY_DATASETS,
    FACT_DATASETS,
    INTEGER_FIELDS,
    NUMERIC_FIELDS,
    SENSITIVE_FIELDS,
    descriptor,
)
from factory_agent.ports.analysis_data import AnalysisValue, FactQuery, FactSource, LoadedDataset
from factory_agent.ports.dictionary import DictionarySource
from factory_agent.ports.result_table import ResultTable
from factory_agent.ports.sandbox import InteractionSandboxPort, SandboxTable


class AnalysisHost:
    def __init__(
        self,
        request: AnalysisRequest,
        fact_source: FactSource,
        dictionary_source: DictionarySource,
        sandbox: InteractionSandboxPort,
        *,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self.request = request
        self.fact_source = fact_source
        self.dictionary_source = dictionary_source
        self.sandbox = sandbox
        self.new_id = new_id or (lambda: uuid4().hex)
        self.datasets: dict[str, LoadedDataset] = {}
        self.results: dict[str, ResultTable] = {}
        self.knowledge: dict[str, KnowledgeSnippet] = {}
        self.observations: dict[int, ApprovedObservation] = {}
        self.ensured: dict[str, ApprovedObservation] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def validate(self, context: RunContext, control: RunControl | None = None) -> None:
        if context.host is not self or self._closed:
            raise ActionRejected("unknown_reference")
        if control is not None:
            control.check()
        if self.request.cancelled():
            raise ActionRejected("cancelled")
        if not await self.request.validate_active():
            raise ActionRejected("denied")
        if self.request.context.tenant_id != self.request.scope.tenant_id:
            raise ActionRejected("denied")

    def observation(self, **values: Any) -> ApprovedObservation:
        result = ApprovedObservation(**values)
        self.observations[id(result)] = result
        return result

    def register_knowledge(self, snippet: KnowledgeSnippet) -> str:
        self.knowledge[snippet.reference] = snippet
        return snippet.reference

    def register_result(self, result: ResultTable) -> str:
        reference = "result:" + self.new_id()
        if reference in self.results:
            raise ActionRejected("execution_failed")
        self.results[reference] = result
        return reference

    def get_dataset(self, reference: str) -> LoadedDataset:
        if self._closed or reference not in self.datasets:
            raise ActionRejected("unknown_reference")
        return self.datasets[reference]

    def get_result(self, reference: str) -> ResultTable:
        if self._closed or reference not in self.results:
            raise ActionRejected("unknown_reference")
        return self.results[reference]

    def close(self) -> None:
        self._closed = True
        self.datasets.clear()
        self.results.clear()
        self.knowledge.clear()
        self.observations.clear()
        self.ensured.clear()
        self.locks.clear()

    def authorize_dataset(self, dataset_id: str) -> None:
        if dataset_id not in DATASETS:
            raise ActionRejected("unavailable")
        if dataset_id in DICTIONARY_DATASETS:
            return
        capability = (
            Capability.ORDER_PROGRESS
            if dataset_id == "production_progress"
            else Capability.OWN_OUTPUT
            if self.request.context.role is Role.EMPLOYEE or self.request.self_only
            else Capability.ORDER_OUTPUT
        )
        if not authorize_capability(capability, self.request.context, self.request.scope).allowed:
            raise ActionRejected("denied")

    def authorize_metric(self, field: str) -> None:
        if field != "payroll_amount":
            return
        capability = (
            Capability.OWN_PAYROLL_SUMMARY
            if self.request.context.role is Role.EMPLOYEE or self.request.self_only
            else Capability.FACTORY_PAYROLL_STATS
            if self.request.context.role is Role.OWNER
            else Capability.TEAM_PAYROLL_LIST
        )
        if not authorize_capability(capability, self.request.context, self.request.scope).allowed:
            raise ActionRejected("denied")


class HostObservationPolicy:
    def __init__(self, host: AnalysisHost) -> None:
        self.host = host

    async def authorize(self, context: RunContext, tool: str, arguments: BaseModel) -> None:
        await self.host.validate(context)

    async def approve(self, context: RunContext, tool: str, result: object) -> ApprovedObservation:
        await self.host.validate(context)
        if (
            not isinstance(result, ApprovedObservation)
            or self.host.observations.get(id(result)) is not result
        ):
            raise ActionRejected("observation_failed")
        return ApprovedObservation.model_validate(result.model_dump())


class HostDatasetProvider:
    def __init__(self, host: AnalysisHost) -> None:
        self.host = host

    async def discover(self, context: RunContext, goal: str, control: RunControl) -> object:
        await self.host.validate(context, control)
        descriptions: list[DatasetDescriptor] = []
        for dataset_id in DATASETS:
            try:
                self.host.authorize_dataset(dataset_id)
            except ActionRejected:
                continue
            descriptions.append(
                descriptor(
                    dataset_id,
                    self.host.request.context.role,
                    self_only=self.host.request.self_only,
                    timezone=self.host.request.factory_timezone,
                )
            )
        return self.host.observation(state="available", descriptors=tuple(descriptions))

    async def describe(self, context: RunContext, dataset_id: str, control: RunControl) -> object:
        await self.host.validate(context, control)
        self.host.authorize_dataset(dataset_id)
        return self.host.observation(
            state="available",
            descriptors=(
                descriptor(
                    dataset_id,
                    self.host.request.context.role,
                    self_only=self.host.request.self_only,
                    timezone=self.host.request.factory_timezone,
                ),
            ),
        )

    async def inspect(self, context: RunContext, dataset_ref: str, control: RunControl) -> object:
        await self.host.validate(context, control)
        loaded = self.host.get_dataset(dataset_ref)
        return self.host.observation(
            state="empty" if not loaded.rows else "available",
            dataset_refs=(loaded.reference,),
            descriptors=(loaded.descriptor,),
            complete=True,
            row_count=len(loaded.rows),
            source="interaction",
        )

    async def ensure(self, context: RunContext, query: Query, control: RunControl) -> object:
        await self.host.validate(context, control)
        self.host.authorize_dataset(query.dataset_id)
        self._validate_query(query)
        query = self._canonical(query)
        fingerprint = hashlib.sha256(query.model_dump_json().encode()).hexdigest()
        lock = self.host.locks.setdefault(fingerprint, asyncio.Lock())
        async with lock:
            existing = self.host.ensured.get(fingerprint)
            if existing is not None:
                with trace_local_operation(
                    "cache",
                    "cache.interaction_exact",
                    input_payload=lambda: {
                        "dataset": query.dataset_id,
                        "request_digest": fingerprint,
                        "dataset_refs": existing.dataset_refs,
                    },
                ) as trace:
                    trace.set_output(
                        outcome="reuse" if existing.complete else "unavailable",
                        source="interaction",
                        output_rows=existing.row_count,
                        complete=existing.complete,
                        metric_counts={"interaction_reuses": 1, "exact_reuses": 1}
                        if existing.complete
                        else {},
                    )
                    if not existing.dataset_refs:
                        return existing
                    return self.host.observation(
                        **{
                            **existing.model_dump(),
                            "state": "interaction_reuse",
                            "source": "interaction",
                        }
                    )
            covering = next(
                (loaded for loaded in self.host.datasets.values() if self._covers(loaded, query)),
                None,
            )
            if covering is not None:
                result = self._reuse(covering, query, control)
            else:
                result = await self._load(query, control)
            self.host.ensured[fingerprint] = result
            return result

    def _canonical(self, query: Query) -> Query:
        updates: dict[str, object] = {
            "columns": tuple(sorted(set(query.columns))),
            "filters": tuple(sorted(query.filters, key=lambda item: item.model_dump_json())),
            "semantics": tuple(sorted(query.semantics, key=lambda item: item.name)),
        }
        if query.dataset_id in FACT_DATASETS:
            window = self._window(query)
            updates.update(window_start=window.start, window_end=window.end)
            semantics = {item.name: item.value for item in query.semantics}
            semantics.setdefault("time_basis", "reported")
            semantics.setdefault("source_type", "all")
            updates["semantics"] = tuple(
                SemanticParameter(name=name, value=value)
                for name, value in sorted(semantics.items())
            )
        return query.model_copy(update=updates)

    @staticmethod
    def _covers(loaded: LoadedDataset, query: Query) -> bool:
        if (
            loaded.query.dataset_id != query.dataset_id
            or not {*query.columns, *(condition.field for condition in query.filters)}
            <= set(loaded.columns)
            or loaded.query.semantics != query.semantics
        ):
            return False
        if query.dataset_id in FACT_DATASETS:
            if (
                loaded.query.window_start is None
                or loaded.query.window_end is None
                or query.window_start is None
                or query.window_end is None
            ):
                return False
            if (
                query.window_start < loaded.query.window_start
                or query.window_end > loaded.query.window_end
            ):
                return False
            if (loaded.query.window_start, loaded.query.window_end) != (
                query.window_start,
                query.window_end,
            ) and "business_date" not in loaded.columns:
                return False
        for prior in loaded.query.filters:
            matching = [condition for condition in query.filters if condition.field == prior.field]
            if not matching or not any(
                set(condition.values) <= set(prior.values) for condition in matching
            ):
                return False
        return True

    def _reuse(
        self, loaded: LoadedDataset, query: Query, control: RunControl
    ) -> ApprovedObservation:
        with trace_local_operation(
            "cache",
            "cache.interaction_covering",
            input_payload=lambda: {
                "dataset": query.dataset_id,
                "dataset_ref": loaded.reference,
                "input_rows": len(loaded.rows),
            },
            timing_role="parent",
        ) as trace:
            with trace_local_operation(
                "cache",
                "cache.interaction_filter",
                input_payload=lambda: {
                    "method": "python",
                    "input_rows": len(loaded.rows),
                    "rules": (
                        "_matches: requested dimension membership; "
                        "business_date inside inclusive calendar window"
                    ),
                    "filter_fields": tuple(condition.field for condition in query.filters),
                    "window_narrowed": (loaded.query.window_start, loaded.query.window_end)
                    != (query.window_start, query.window_end),
                },
            ) as filter_trace:
                rows = tuple(row for row in loaded.rows if self._matches(row, query))
                if query.dataset_id in FACT_DATASETS and (
                    loaded.query.window_start,
                    loaded.query.window_end,
                ) != (query.window_start, query.window_end):
                    window = self._window(query)
                    dates, datee = local_calendar_dates(
                        window.start, window.end, ZoneInfo(self.host.request.factory_timezone)
                    )
                    rows = tuple(
                        row for row in rows if dates <= str(row.get("business_date")) <= datee
                    )
                filter_trace.set_output(outcome="filtered", output_rows=len(rows))
            if len(rows) > query.limit:
                trace.set_status("skipped", reason="row_limit")
                trace.set_output(outcome="unavailable", output_rows=len(rows), complete=False)
                return self.host.observation(
                    state="incomplete", complete=False, reason_code="incomplete"
                )
            if rows == loaded.rows:
                trace.set_output(
                    outcome="reuse",
                    output_rows=len(rows),
                    complete=True,
                    source="interaction",
                    dataset_refs=(loaded.reference,),
                    metric_counts={"interaction_reuses": 1, "covering_reuses": 1},
                )
                return self.host.observation(
                    state="interaction_reuse",
                    dataset_refs=(loaded.reference,),
                    descriptors=(loaded.descriptor,),
                    row_count=len(rows),
                    complete=True,
                    source="interaction",
                )
            control.consume("processed_rows", len(rows))
            reference = "dataset:" + self.host.new_id()
            if reference in self.host.datasets:
                raise ActionRejected("execution_failed")
            table_name = "analysis_data_" + str(len(self.host.datasets))
            self.host.sandbox.register_table(
                SandboxTable(
                    table_name,
                    tuple(dict(row) for row in rows),
                    tuple(
                        (
                            field,
                            "DECIMAL(28,8)"
                            if field in NUMERIC_FIELDS
                            else "DATE"
                            if field == "business_date"
                            else "BOOLEAN"
                            if field in BOOLEAN_FIELDS
                            else "BIGINT"
                            if field in INTEGER_FIELDS
                            else "VARCHAR",
                        )
                        for field in loaded.columns
                    ),
                )
            )
            view_descriptor = loaded.descriptor.model_copy(
                update={"window_start": query.window_start, "window_end": query.window_end}
            )
            self.host.datasets[reference] = LoadedDataset(
                reference, query, table_name, rows, loaded.columns, view_descriptor
            )
            trace.set_output(
                outcome="reuse",
                output_rows=len(rows),
                complete=True,
                source="interaction",
                dataset_refs=(reference,),
                metric_counts={"interaction_reuses": 1, "covering_reuses": 1},
            )
            return self.host.observation(
                state="interaction_reuse",
                dataset_refs=(reference,),
                descriptors=(view_descriptor,),
                row_count=len(rows),
                complete=True,
                source="interaction",
            )

    def _validate_query(self, query: Query) -> None:
        spec = DATASETS[query.dataset_id]
        if any(field not in spec.fields for field in query.columns):
            raise ActionRejected("invalid_action")
        for condition in query.filters:
            if condition.field not in spec.fields or condition.field in SENSITIVE_FIELDS:
                raise ActionRejected("invalid_action")
            if condition.operator not in {"eq", "in"}:
                raise ActionRejected("invalid_action")
            if condition.field in {"bed_code", "bill_code", "style_id", "style_code", "plan_code"}:
                if any(not isinstance(value, str) for value in condition.values):
                    raise ActionRejected("invalid_action")
        semantic_names = [item.name for item in query.semantics]
        if len(semantic_names) != len(set(semantic_names)):
            raise ActionRejected("invalid_action")
        for item in query.semantics:
            if item.name not in {"time_basis", "source_type"}:
                raise ActionRejected("invalid_action")
            if item.name == "time_basis" and item.value not in {"reported", "audited"}:
                raise ActionRejected("invalid_action")
            if item.name == "source_type" and item.value not in {
                "all",
                "scan",
                "hanging",
                "manual",
            }:
                raise ActionRejected("invalid_action")
        if query.dataset_id in DICTIONARY_DATASETS and query.semantics:
            raise ActionRejected("invalid_action")

    async def _load(self, query: Query, control: RunControl) -> ApprovedObservation:
        required = tuple(dict.fromkeys((*query.columns, *(item.field for item in query.filters))))
        if self.host.request.slots.style_codes and query.dataset_id in {
            "styles",
            "style_processes",
        }:
            required = tuple(dict.fromkeys((*required, "style_code")))
        source: str
        generated_at = None
        if query.dataset_id in FACT_DATASETS:
            if self.host.request.validate_fact_scope is not None:
                if not await self.host.request.validate_fact_scope(query.dataset_id):
                    raise ActionRejected("denied")
            window = self._window(query)
            filters = self._fact_filters(query)
            required = tuple(dict.fromkeys((*required, "business_date")))
            if query.dataset_id == "reported_production" and not query.columns:
                required = tuple(dict.fromkeys((*required, "business_date", "quantity")))
            if filters.bed_codes is not None:
                required = tuple(dict.fromkeys((*required, "bed_code")))
            semantics = {item.name: item.value for item in query.semantics}
            batch = await self.host.fact_source.fetch(
                FactQuery(
                    dataset_id=query.dataset_id,
                    columns=required,
                    filters=filters,
                    scope=self.host.request.scope,
                    role=self.host.request.context.role,
                    time_range=window,
                    time_basis=semantics.get("time_basis", "reported"),
                    source_type=semantics.get("source_type", "all"),
                )
            )
            control.check()
            if not batch.complete or batch.missing_fields or batch.invalid_fields:
                return self.host.observation(
                    state="unavailable",
                    complete=False,
                    missing_fields=batch.missing_fields,
                    reason_code="unavailable",
                )
            rows = batch.rows
            generated_at = batch.generated_at
            source = batch.cache_source
        else:
            entities = self._entities(query)
            directory = await self.host.dictionary_source.fetch(
                query.dataset_id, self.host.request.scope, entities
            )
            control.check()
            if not directory.complete:
                return self.host.observation(
                    state="unavailable", complete=False, reason_code="incomplete"
                )
            rows = directory.rows
            generated_at = directory.generated_at
            source = directory.cache_source
        control.consume("processed_rows", len(rows))
        missing = tuple(field for field in required if any(field not in row for row in rows))
        if missing:
            return self.host.observation(
                state="unavailable",
                complete=False,
                missing_fields=missing,
                reason_code="unavailable",
            )
        rows = tuple(row for row in rows if self._matches(row, query))
        if len(rows) > query.limit:
            return self.host.observation(
                state="incomplete", complete=False, reason_code="incomplete"
            )
        columns = (
            (required or DATASETS[query.dataset_id].fields)
            if not rows
            else tuple(
                field
                for field in DATASETS[query.dataset_id].fields
                if all(field in row for row in rows)
            )
        )
        for field in tuple(columns):
            if field not in NUMERIC_FIELDS | INTEGER_FIELDS | BOOLEAN_FIELDS:
                continue
            try:
                self._normalize(rows, (field,))
            except ValueError:
                if field in required:
                    return self.host.observation(
                        state="unavailable", complete=False, reason_code="unavailable"
                    )
                columns = tuple(column for column in columns if column != field)
        try:
            normalized = self._normalize(rows, columns)
        except ValueError:
            return self.host.observation(
                state="unavailable", complete=False, reason_code="unavailable"
            )
        reference = "dataset:" + self.host.new_id()
        table_name = "analysis_data_" + str(len(self.host.datasets))
        if reference in self.host.datasets:
            raise ActionRejected("execution_failed")
        safe_descriptor = descriptor(
            query.dataset_id,
            self.host.request.context.role,
            self_only=self.host.request.self_only,
            timezone=self.host.request.factory_timezone,
        ).model_copy(
            update={
                "complete": True,
                "available": True,
                "generated_at": generated_at,
                "window_start": query.window_start
                or (
                    self.host.request.time_range.start
                    if query.dataset_id in FACT_DATASETS and self.host.request.time_range
                    else None
                ),
                "window_end": query.window_end
                or (
                    self.host.request.time_range.end
                    if query.dataset_id in FACT_DATASETS and self.host.request.time_range
                    else None
                ),
                "fields": tuple(
                    field.model_copy(update={"validated": field.name in columns})
                    for field in descriptor(
                        query.dataset_id,
                        self.host.request.context.role,
                        self_only=self.host.request.self_only,
                        timezone=self.host.request.factory_timezone,
                    ).fields
                ),
            }
        )
        self.host.sandbox.register_table(
            SandboxTable(
                table_name,
                tuple(dict(row) for row in normalized),
                tuple(
                    (
                        field,
                        "DECIMAL(28,8)"
                        if field in NUMERIC_FIELDS
                        else "DATE"
                        if field == "business_date"
                        else "BOOLEAN"
                        if field in BOOLEAN_FIELDS
                        else "BIGINT"
                        if field in INTEGER_FIELDS
                        else "VARCHAR",
                    )
                    for field in columns
                ),
            )
        )
        self.host.datasets[reference] = LoadedDataset(
            reference, query, table_name, normalized, columns, safe_descriptor
        )
        return self.host.observation(
            state="empty"
            if not rows
            else "cache_hit"
            if source in {"cache", "precompute", "query"}
            else "live_fetch",
            dataset_refs=(reference,),
            descriptors=(safe_descriptor,),
            complete=True,
            row_count=len(rows),
            source="cache" if source in {"cache", "precompute", "query"} else "upstream",
        )

    def _window(self, query: Query) -> TimeRange:
        original = self.host.request.time_range
        if original is None:
            raise ActionRejected("unavailable")
        selected = (
            TimeRange(query.window_start, query.window_end)
            if query.window_start is not None and query.window_end is not None
            else original
        )
        if selected.start < original.start or selected.end > original.end:
            raise ActionRejected("denied")
        zone = ZoneInfo(self.host.request.factory_timezone)
        if any(
            bound.astimezone(zone).time().isoformat() != "00:00:00"
            for bound in (
                selected.start,
                selected.end,
            )
        ):
            raise ActionRejected("unavailable")
        return selected

    def _fact_filters(self, query: Query) -> NarrowedFilters:
        slots = self.host.request.slots
        base = self.host.request.filters
        values: dict[str, frozenset[str] | None] = {
            "bed_code": base.bed_codes if base is not None else frozenset(slots.bed_codes) or None,
            "style_code": base.style_codes
            if base is not None
            else frozenset(slots.style_codes) or None,
            "plan_code": base.plan_codes
            if base is not None
            else frozenset((*slots.plan_codes, *slots.order_codes)) or None,
        }
        for condition in query.filters:
            if condition.field not in values:
                continue
            requested = frozenset(str(value) for value in condition.values)
            current = values[condition.field]
            values[condition.field] = current & requested if current is not None else requested
            if not values[condition.field]:
                raise ActionRejected("denied")
        if base is not None:
            return replace(
                base,
                bed_codes=values["bed_code"],
                style_codes=values["style_code"],
                plan_codes=values["plan_code"],
            )
        return FilterNarrower().narrow(
            self.host.request.scope,
            order_ids=frozenset(slots.order_codes) or None,
            plan_ids=values["plan_code"],
            style_ids=values["style_code"],
            bed_codes=values["bed_code"],
            restrict_to_scope_employees=self.host.request.context.role is Role.EMPLOYEE
            or self.host.request.self_only,
        )

    def _entities(self, query: Query) -> tuple[str, ...]:
        if query.dataset_id == "bill_processes":
            authorized = {
                str(row["bill_code"])
                for dataset in self.host.datasets.values()
                if dataset.query.dataset_id == "production_progress"
                for row in dataset.rows
                if row.get("bill_code")
            }
            selected = {
                str(value)
                for condition in query.filters
                if condition.field == "bill_code"
                for value in condition.values
            }
            if not authorized or (selected and not selected <= authorized):
                raise ActionRejected("denied")
            return tuple(sorted(selected or authorized))
        if query.dataset_id in {"style_colors", "style_sizes"}:
            authorized = self._target_style_ids() | {
                str(row["style_id"])
                for dataset in self.host.datasets.values()
                if dataset.query.dataset_id == "styles"
                and (
                    self.host.request.slots.style_codes
                    or any(
                        condition.field in {"style_code", "style_id"}
                        for condition in dataset.query.filters
                    )
                )
                for row in dataset.rows
                if row.get("style_id")
            }
            selected = {
                str(value)
                for condition in query.filters
                if condition.field == "style_id"
                for value in condition.values
            }
            if not authorized:
                raise ActionRejected("unavailable")
            if selected and not selected <= authorized:
                raise ActionRejected("denied")
            return tuple(sorted(selected or authorized))
        return ()

    def _target_style_ids(self) -> set[str]:
        if not self.host.request.slots.bed_codes:
            return set()
        authorized = {
            str(row["style_id"])
            for dataset in self.host.datasets.values()
            if dataset.query.dataset_id == "production_progress"
            for row in dataset.rows
            if row.get("style_id")
        }
        if not authorized:
            raise ActionRejected("unavailable")
        return authorized

    def _matches(self, row: Mapping[str, AnalysisValue], query: Query) -> bool:
        if self.host.request.slots.bed_codes and query.dataset_id in {
            "styles",
            "style_processes",
            "style_colors",
            "style_sizes",
        }:
            if str(row.get("style_id", "")) not in self._target_style_ids():
                return False
        if self.host.request.slots.style_codes and query.dataset_id in {
            "styles",
            "style_processes",
        }:
            if row.get("style_code") not in self.host.request.slots.style_codes:
                return False
        return all(
            any(
                row.get(item.field)
                == (
                    str(value)
                    if isinstance(row.get(item.field), str) and not isinstance(value, str)
                    else value
                )
                for value in item.values
            )
            for item in query.filters
        )

    @staticmethod
    def _normalize(
        rows: tuple[Mapping[str, AnalysisValue], ...], columns: tuple[str, ...]
    ) -> tuple[Mapping[str, AnalysisValue], ...]:
        output: list[Mapping[str, AnalysisValue]] = []
        for row in rows:
            record: dict[str, AnalysisValue] = {}
            for column in columns:
                value = row[column]
                if column in NUMERIC_FIELDS:
                    if value is None or isinstance(value, bool) or str(value) == "":
                        raise ValueError("numeric contribution is unavailable")
                    try:
                        value = Decimal(str(value))
                    except InvalidOperation:
                        raise ValueError("numeric contribution is invalid") from None
                    if not value.is_finite():
                        raise ValueError("numeric contribution is invalid")
                    try:
                        representable = abs(value) < Decimal("1e20") and value == value.quantize(
                            Decimal("0.00000001")
                        )
                    except InvalidOperation:
                        representable = False
                    if not representable:
                        raise ValueError(
                            "numeric contribution exceeds the reviewed storage precision"
                        )
                elif column in INTEGER_FIELDS:
                    if isinstance(value, bool) or value is None:
                        raise ValueError("configuration order is invalid")
                    try:
                        integer = int(str(value))
                    except ValueError:
                        raise ValueError("configuration order is invalid") from None
                    if not -(2**63) <= integer < 2**63:
                        raise ValueError("configuration order is invalid")
                    value = integer
                elif column in BOOLEAN_FIELDS:
                    if not isinstance(value, bool):
                        raise ValueError("configuration state is invalid")
                elif column == "business_date":
                    try:
                        value = date.fromisoformat(str(value)).isoformat()
                    except ValueError:
                        raise ValueError("business date is invalid") from None
                elif value is not None:
                    value = str(value)
                record[column] = value
            output.append(record)
        return tuple(output)
