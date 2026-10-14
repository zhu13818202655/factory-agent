from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from factory_agent.domain import Role
from factory_agent.domain.errors import ForbiddenError, InvalidRequestError
from factory_agent.ports.analysis_data import AnalysisValue, FactBatch, FactQuery
from factory_agent.ports.contracts import Clock, ResourceFetchResult
from factory_agent.ports.resource_execution import ExecutionRequest

_COMMON = {
    "plan_code": "jhdh",
    "bed_code": "chuanghao",
    "style_id": "huohao",
    "style_code": "bbreed",
    "process_id": "worktype",
    "process_name": "wtname",
    "color": "color",
    "size": "chima",
    "department": "deptname",
}
_FIELDS = {
    "reported_production": {
        **_COMMON,
        "business_date": "inputdate",
        "quantity": "sl",
        "payroll_amount": "je",
        "source_type": "type",
    },
    "production_progress": {
        **_COMMON,
        "business_date": "rq",
        "bill_code": "dh",
        "package_code": "baohao",
        "issued_quantity": "fhsl",
        "received_quantity": "sssl",
    },
}
_OPERATIONS = {"reported_production": "GongziClQuery", "production_progress": "ScjdQuery"}
_NUMERIC = frozenset({"quantity", "payroll_amount", "issued_quantity", "received_quantity"})
_FLAGS = {"reported": "0", "audited": "1", "pending": ""}
_TYPES = {"all": "0,1,2", "scan": "0", "hanging": "1", "manual": "2"}


class _Executor(Protocol):
    async def execute_full_step(
        self,
        filters: Any,
        request: ExecutionRequest,
        active_scope: Any | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ResourceFetchResult: ...


class CachedFactSource:
    def __init__(
        self,
        executor: _Executor,
        clock: Clock,
        *,
        page_size: int = 200,
        factory_timezone: str = "Asia/Shanghai",
    ) -> None:
        self._executor = executor
        self._clock = clock
        self._page_size = page_size
        self._timezone = ZoneInfo(factory_timezone)

    async def fetch(self, query: FactQuery) -> FactBatch:
        if query.filters.tenant_id != query.scope.tenant_id:
            raise ForbiddenError("analysis_scope_mismatch")
        if query.role is Role.EMPLOYEE and (
            not query.filters.employee_ids
            or not query.filters.employee_ids <= query.scope.employee_ids
        ):
            raise ForbiddenError("analysis_scope_mismatch")
        if not query.scope.mes_filtered and (
            (
                query.filters.employee_ids is not None
                and not set(query.filters.employee_ids) <= set(query.scope.employee_ids)
            )
            or (
                query.filters.requested_dept_ids is not None
                and not set(query.filters.requested_dept_ids) <= set(query.scope.dept_ids)
            )
        ):
            raise ForbiddenError("analysis_scope_mismatch")
        if (
            query.dataset_id not in _FIELDS
            or not set(query.columns) <= _FIELDS[query.dataset_id].keys()
        ):
            raise InvalidRequestError("analysis_dataset_invalid")
        if query.time_basis not in _FLAGS or query.source_type not in _TYPES:
            raise InvalidRequestError("analysis_semantics_invalid")
        params: dict[str, Any] = {}
        fields = dict(_FIELDS[query.dataset_id])
        if query.dataset_id == "reported_production":
            params = {"Flag": _FLAGS[query.time_basis], "Type": _TYPES[query.source_type]}
            if query.time_basis == "audited":
                fields["business_date"] = "check_time"
            elif query.time_basis == "pending":
                # Pending rows have no reviewed analysis date anchor.
                return FactBatch(query.dataset_id, (), False, reason="unavailable_time_basis")
        elif query.time_basis != "reported" or query.source_type != "all":
            raise InvalidRequestError("analysis_semantics_invalid")
        fetched = await self._executor.execute_full_step(
            query.filters,
            ExecutionRequest(
                _OPERATIONS[query.dataset_id],
                (query.time_range.start, query.time_range.end),
                self._page_size,
                query.role,
                result_validator=lambda result: _required_fields_valid(result, query, fields),
                allow_owner_snapshot=False,
            ),
            query.scope,
            params,
        )
        if fetched.shadow_result is not None:
            shadow = fetched.shadow_result
            valid = _required_fields_valid(shadow, query, fields)
            consistent = valid and _approved_rows(fetched, query, fields) == _approved_rows(
                shadow, query, fields
            )
            if not consistent:
                discard = getattr(self._executor, "discard_cached_result", None)
                if discard is not None:
                    await discard(fetched)
            else:
                release = getattr(self._executor, "release_cached_result", None)
                if release is not None:
                    release(fetched)
            fetched = replace(
                shadow,
                complete=shadow.complete and valid,
                source_call_count=fetched.source_call_count,
                shadow_result=None,
            )
        missing: set[str] = set()
        invalid: set[str] = set()
        projected: list[Mapping[str, AnalysisValue]] = []
        for row in fetched.rows:
            if not _scope_contains(row, query):
                continue
            values: dict[str, AnalysisValue] = {}
            for column in fields:
                original = fields[column]
                if original not in row:
                    if column in query.columns:
                        missing.add(column)
                    continue
                raw = row[original]
                if column in _NUMERIC:
                    try:
                        numeric = Decimal(str(raw))
                        if not numeric.is_finite():
                            raise InvalidOperation
                        if abs(numeric) >= Decimal("1e20") or numeric != numeric.quantize(
                            Decimal("0.00000001")
                        ):
                            raise InvalidOperation
                        values[column] = numeric
                    except (InvalidOperation, ValueError):
                        if column in query.columns:
                            invalid.add(column)
                elif column == "business_date":
                    try:
                        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        if value.tzinfo is not None:
                            value = value.astimezone(self._timezone)
                        values[column] = value.date().isoformat()
                    except ValueError:
                        if column in query.columns:
                            invalid.add(column)
                elif isinstance(raw, str):
                    values[column] = raw
                else:
                    if column in query.columns:
                        invalid.add(column)
            projected.append(MappingProxyType(values))
        generated = self._clock.now()
        if fetched.cache_generated_at:
            try:
                generated = datetime.fromisoformat(fetched.cache_generated_at)
            except ValueError:
                generated = None
        return FactBatch(
            query.dataset_id,
            tuple(projected),
            fetched.complete,
            tuple(sorted(missing)),
            tuple(sorted(invalid)),
            generated,
            "cache" if fetched.cache_source else "live",
            fetched.source_call_count
            if fetched.source_call_count is not None
            else int(fetched.cache_source is None),
            fetched.reason,
        )


def _scope_contains(row: Mapping[str, object], query: FactQuery) -> bool:
    employee_ids = query.filters.employee_ids
    if employee_ids is not None:
        if "uid" not in row or not str(row["uid"]):
            raise ForbiddenError("analysis_scope_unproven")
        if str(row["uid"]) not in employee_ids:
            return False
    department_ids = query.filters.requested_dept_ids
    if query.role in {Role.MANAGER, Role.GROUP_LEADER}:
        department_ids = query.scope.dept_ids if department_ids is None else department_ids
    if department_ids is not None:
        if "dept" not in row:
            raise ForbiddenError("analysis_scope_unproven")
        if str(row["dept"]) not in department_ids:
            return False
    return True


def _approved_rows(
    result: ResourceFetchResult, query: FactQuery, fields: Mapping[str, str]
) -> Counter[tuple[str, ...]]:
    return Counter(
        tuple(str(row.get(original, "")) for original in fields.values())
        for row in result.rows
        if _scope_contains(row, query)
    )


def _required_fields_valid(
    result: ResourceFetchResult, query: FactQuery, fields: Mapping[str, str]
) -> bool:
    for row in result.rows:
        if not _scope_contains(row, query):
            continue
        for column in query.columns:
            if fields[column] not in row:
                return False
            value = row[fields[column]]
            if column in _NUMERIC:
                try:
                    number = Decimal(str(value))
                    if (
                        not number.is_finite()
                        or abs(number) >= Decimal("1e20")
                        or number != number.quantize(Decimal("0.00000001"))
                    ):
                        return False
                except InvalidOperation:
                    return False
            elif column == "business_date":
                try:
                    datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError:
                    return False
            elif not isinstance(value, str):
                return False
    return True
