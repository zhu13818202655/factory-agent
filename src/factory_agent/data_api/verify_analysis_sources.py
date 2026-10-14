"""Explicitly invoked, read-only source verification for an authorized TEST environment.

Only aggregate validation metadata is emitted. No model, database, persistent
cache, trace store or export is opened, and customer values never leave this
module. The command must be invoked with ``--test-environment`` after the
operator has confirmed that the configured MES is a permitted test system.
"""

import argparse
import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.directory import MesDirectorySource
from factory_agent.data_api.hongzhao import (
    AdapterSettings,
    HongzhaoMesAdapter,
    MesRequest,
    MesResponse,
)
from factory_agent.data_api.knowledge_directory import (
    DictionaryValidationError,
    MesDictionarySource,
)
from factory_agent.data_api.pagination import PagerBudget
from factory_agent.data_api.result_cache import scope_filtered_rows
from factory_agent.data_api.schemas import GongziClRow, MesEnvelope, ScjdV2Row
from factory_agent.data_api.token_gateway import (
    GatewayTokenRefresher,
    TokenBackedMembershipResolver,
    TokenCredentialExchange,
)
from factory_agent.domain.errors import ForbiddenError, InvalidRequestError, MesError
from factory_agent.observability.debug_trace import suspend_debug_capture
from factory_agent.ports.contracts import ResourceFetchResult
from factory_agent.ports.dictionary import DictionaryBatch
from factory_agent.ports.run_budget import bind_run_budget
from factory_agent.ports.source_verification import (
    SourceAuthorization as ResolvedAuthorization,
)
from factory_agent.ports.source_verification import (
    VerificationPolicy,
)

_DATASETS = frozenset(
    {
        "styles",
        "style_types",
        "production_types",
        "processes",
        "style_processes",
        "style_colors",
        "style_sizes",
        "bill_processes",
        "hanging_lines",
        "reported_production",
        "production_progress",
    }
)
_FACT_FIELD_NAMES = {
    "chuanghao": "bed_code",
    "huohao": "style_id",
    "bbreed": "style_code",
    "dh": "bill_code",
    "jhdh": "plan_code",
    "worktype": "process_id",
    "rq": "business_date",
    "inputdate": "business_date",
    "sl": "quantity",
    "fhsl": "issued_quantity",
    "sssl": "received_quantity",
    "je": "payroll_amount",
    "uid": "identity",
    "dept": "scope",
    "check_time": "business_date",
}


class _VerificationAdapter(HongzhaoMesAdapter):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.validation_fields: dict[str, tuple[str, ...]] = {}
        self.current_operation = ""

    async def execute(self, request: MesRequest) -> MesResponse:
        self.current_operation = request.operation_id
        async with asyncio.timeout(12):
            response = await super().execute(request)
        model = (
            GongziClRow
            if request.operation_id == "GongziClQuery"
            else ScjdV2Row
            if request.operation_id == "ScjdQuery"
            else None
        )
        result_payload: object = response.result
        if model is None or not isinstance(result_payload, dict):
            return response
        payload = cast(dict[str, object], result_payload)
        rows = payload.get("list")
        if not isinstance(rows, list):
            self.validation_fields[request.operation_id] = ("row_collection",)
            return response
        for row in cast(list[object], rows)[:20000]:
            try:
                model.model_validate(row)
            except ValidationError as error:
                fields: set[str] = set()
                for detail in error.errors(
                    include_input=False, include_context=False, include_url=False
                ):
                    raw = detail["loc"][-1] if detail["loc"] else ""
                    logical = _FACT_FIELD_NAMES.get(str(raw), "row_shape")
                    if isinstance(row, dict):
                        candidate = cast(dict[str, object], row).get(str(raw))
                        kind = (
                            "integer"
                            if type(candidate) is int
                            else "decimal"
                            if (type(candidate) is float)
                            else "boolean"
                            if type(candidate) is bool
                            else "string"
                            if (isinstance(candidate, str))
                            else "missing"
                            if candidate is None
                            else "invalid_shape"
                        )
                        logical += "." + kind
                    fields.add(logical)
                self.validation_fields[request.operation_id] = tuple(sorted(fields))
                break
        return response

    def _map_status(self, response: httpx.Response) -> MesEnvelope:
        try:
            envelope = cast(MesEnvelope, super()._map_status(response))
        except MesError:
            self.validation_fields[self.current_operation] = ("response_schema",)
            raise
        if envelope.code != 1:
            self.validation_fields[self.current_operation] = ("upstream_rejected",)
        return envelope


class VerificationSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    base_url: AnyHttpUrl = Field(validation_alias="FACTORY_AGENT_CANONICAL_MES_BASE_URL")
    credential: SecretStr = Field(
        validation_alias=AliasChoices(
            "FACTORY_AGENT_MES_USER_CREDENTIAL_99",
            "MES_USER_CREDENTIAL_99",
        )
    )


@dataclass(frozen=True, slots=True)
class SourceVerification:
    dataset_id: str
    category: str
    row_count: int = 0
    complete: bool = False
    missing_fields: tuple[str, ...] = ()
    duration_ms: int = 0


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class VerificationBudget:
    def __init__(self, *, timeout_seconds: float = 180.0, max_requests: int = 40) -> None:
        self._deadline = time.monotonic() + timeout_seconds
        self._max_requests = max_requests
        self._requests = 0
        self._pages = 0

    def check(self) -> None:
        if time.monotonic() >= self._deadline:
            raise InvalidRequestError("verification_time_budget_exhausted")

    def consume(self, resource: str, amount: int = 1) -> None:
        self.check()
        if resource == "mes_requests":
            self._requests += amount
            if self._requests > self._max_requests:
                raise InvalidRequestError("verification_request_budget_exhausted")
        elif resource == "mes_pages":
            self._pages += amount
            if self._pages > self._max_requests:
                raise InvalidRequestError("verification_page_budget_exhausted")


def _category(error: Exception) -> str:
    if isinstance(error, MesError):
        return error.code.value
    if isinstance(error, TimeoutError):
        return "verification_timeout"
    if isinstance(error, ValueError):
        return "configuration_invalid"
    return "verification_failed"


async def _verify_dictionary(
    source: MesDictionarySource,
    dataset_id: str,
    authorization: ResolvedAuthorization,
    *,
    entity_ids: tuple[str, ...] = (),
) -> tuple[SourceVerification, DictionaryBatch | None]:
    started = time.monotonic()
    try:
        batch = await source.fetch(dataset_id, authorization.data_scope, entity_ids)
        report = SourceVerification(
            dataset_id,
            "validated" if batch.complete else batch.reason or "incomplete",
            row_count=len(batch.rows),
            complete=batch.complete,
        )
    except Exception as error:  # noqa: BLE001 - emit only the approved error category
        batch = None
        report = SourceVerification(
            dataset_id,
            _category(error),
            missing_fields=error.fields if isinstance(error, DictionaryValidationError) else (),
        )
    return SourceVerification(
        report.dataset_id,
        report.category,
        report.row_count,
        report.complete,
        report.missing_fields,
        round((time.monotonic() - started) * 1000),
    ), batch


async def _verify_fact(
    adapter: _VerificationAdapter,
    authorization: ResolvedAuthorization,
    dataset_id: str,
    day: date,
    policy: VerificationPolicy,
) -> tuple[SourceVerification, tuple[Mapping[str, object], ...]]:
    progress = dataset_id == "production_progress"
    filters = policy.narrow_fact(authorization, dataset_id)
    if filters is None:
        return SourceVerification(dataset_id, "forbidden"), ()
    started = time.monotonic()
    rows: tuple[Mapping[str, object], ...] = ()
    try:
        zone = ZoneInfo("Asia/Shanghai")
        start = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(UTC)
        end = start + timedelta(days=1)
        if not 0 < (datetime.now(zone).date() - day).days <= 365:
            raise InvalidRequestError("verification_window_is_outside_historical_bounds")
        operation = "ScjdQuery" if progress else "GongziClQuery"
        fetched: ResourceFetchResult = await adapter.fetch_resource(
            operation,
            filters,
            (start, end),
            200,
            extra_params={} if progress else {"Flag": "0", "Type": "0,1,2"},
        )
        rows = scope_filtered_rows(
            fetched.rows, operation, filters, authorization.tenant_context.role
        )
        fields: dict[str, str] = (
            {
                "dh": "bill_code",
                "rq": "business_date",
                "chuanghao": "bed_code",
                "huohao": "style_id",
                "bbreed": "style_code",
                "worktype": "process_id",
                "fhsl": "issued_quantity",
                "sssl": "received_quantity",
            }
            if progress
            else {
                "inputdate": "business_date",
                "chuanghao": "bed_code",
                "huohao": "style_id",
                "bbreed": "style_code",
                "worktype": "process_id",
                "sl": "quantity",
            }
        )
        missing = tuple(
            sorted(
                {
                    logical
                    for field, logical in fields.items()
                    if any(row.get(field) in (None, "") for row in rows)
                }
            )
        )
        category = (
            fetched.reason or "incomplete"
            if not fetched.complete
            else "missing_required_fields"
            if missing
            else "validated_empty"
            if not rows
            else "validated"
        )
        report = SourceVerification(
            dataset_id,
            category,
            row_count=len(rows),
            complete=fetched.complete,
            missing_fields=missing,
        )
    except Exception as error:  # noqa: BLE001 - customer exception text is never emitted
        report = SourceVerification(
            dataset_id,
            _category(error),
            missing_fields=adapter.validation_fields.get(
                "ScjdQuery" if progress else "GongziClQuery", ()
            ),
        )
        rows = ()
    return SourceVerification(
        report.dataset_id,
        report.category,
        report.row_count,
        report.complete,
        report.missing_fields,
        round((time.monotonic() - started) * 1000),
    ), rows


def _first_entity(rows: tuple[Mapping[str, object], ...], field: str) -> tuple[str, ...]:
    for row in rows:
        candidate = row.get(field)
        if isinstance(candidate, str) and candidate and candidate == candidate.strip():
            return (candidate,)
    return ()


def _credential_value(secret: SecretStr) -> str:
    value = secret.get_secret_value().strip()
    if value.startswith("["):
        values: object = json.loads(value)
        if not isinstance(values, list):
            raise InvalidRequestError("verification_credential_configuration_invalid")
        candidates = cast(list[object], values)
        if not candidates or not isinstance(candidates[0], str):
            raise InvalidRequestError("verification_credential_configuration_invalid")
        value = candidates[0]
    if not value:
        raise InvalidRequestError("verification_credential_missing")
    return value


async def verify_sources(
    settings: VerificationSettings,
    day: date,
    *,
    policy: VerificationPolicy,
    transport: httpx.AsyncBaseTransport | None = None,
    emit: Callable[[SourceVerification], None] | None = None,
    datasets: tuple[str, ...] | None = None,
) -> tuple[SourceVerification, ...]:
    selected = set(datasets or _DATASETS)
    if not selected <= _DATASETS:
        raise InvalidRequestError("verification_dataset_is_not_approved")
    if selected & {"style_colors", "style_sizes", "style_processes"}:
        selected.add("styles")
    if "bill_processes" in selected:
        selected.add("production_progress")
    reports: list[SourceVerification] = []

    def report(item: SourceVerification) -> None:
        reports.append(item)
        if emit is not None:
            emit(item)

    budget = VerificationBudget()
    clock = _Clock()
    async with (
        asyncio.timeout(180),
        httpx.AsyncClient(
            base_url=str(settings.base_url),
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client,
    ):
        with bind_run_budget(budget), suspend_debug_capture():
            exchange = TokenCredentialExchange(str(settings.base_url), client=client, clock=clock)
            try:
                async with asyncio.timeout(12):
                    principal = await exchange.authenticate(_credential_value(settings.credential))
                bundle = exchange.bundle_for(principal.credential)
                if bundle is None:
                    raise ForbiddenError("verification_has_no_bound_identity")
                adapter = _VerificationAdapter(
                    str(settings.base_url),
                    bundle,
                    load_catalog(),
                    refresher=GatewayTokenRefresher(exchange),
                    client=client,
                    clock=clock,
                    settings=AdapterSettings(timeout_seconds=10.0, max_retries=0),
                    pager_budget=PagerBudget(max_pages=20, max_rows=20000, page_size=200),
                )
                with exchange.bind_for(principal.credential):
                    authorization = await policy.authorize(
                        principal.credential,
                        TokenBackedMembershipResolver(exchange),
                        MesDirectorySource(adapter, load_catalog()),
                        clock.now(),
                    )
                    source = MesDictionarySource(
                        adapter,
                        None,
                        clock,
                        before_fetch=budget.check,
                        max_rows=20000,
                        max_entities=1,
                        max_pages=20,
                    )
                    styles: DictionaryBatch | None = None
                    style_processes: DictionaryBatch | None = None
                    for dataset in (
                        "styles",
                        "style_types",
                        "production_types",
                        "processes",
                        "style_processes",
                        "hanging_lines",
                    ):
                        if dataset not in selected:
                            continue
                        item, batch = await _verify_dictionary(source, dataset, authorization)
                        report(item)
                        if dataset == "styles":
                            styles = batch
                        elif dataset == "style_processes":
                            style_processes = batch
                    if (
                        styles is not None
                        and styles.complete
                        and (style_processes is not None and style_processes.complete)
                    ):
                        names = {row["style_id"]: row["style_code"] for row in styles.rows}
                        invalid = sum(
                            names.get(row["style_id"]) != row["style_code"]
                            for row in style_processes.rows
                        )
                        report(
                            SourceVerification(
                                "style_process_relationships",
                                "style_reference_conflict" if invalid else "validated",
                                row_count=len(style_processes.rows),
                                complete=invalid == 0,
                            )
                        )
                    elif "style_processes" in selected:
                        report(SourceVerification("style_process_relationships", "unavailable"))
                    style_ids = _first_entity(styles.rows, "style_id") if styles else ()
                    for dataset in ("style_colors", "style_sizes"):
                        if dataset not in selected:
                            continue
                        if style_ids:
                            item, _ = await _verify_dictionary(
                                source, dataset, authorization, entity_ids=style_ids
                            )
                            report(item)
                        else:
                            report(SourceVerification(dataset, "no_validated_entity"))
                    if "reported_production" in selected:
                        fact, _ = await _verify_fact(
                            adapter, authorization, "reported_production", day, policy
                        )
                        report(fact)
                    if "production_progress" not in selected:
                        return tuple(reports)
                    progress, progress_rows = await _verify_fact(
                        adapter, authorization, "production_progress", day, policy
                    )
                    report(progress)
                    if "bill_processes" not in selected:
                        return tuple(reports)
                    bill_ids = (
                        _first_entity(progress_rows, "dh")
                        if progress.category == "validated"
                        else ()
                    )
                    if bill_ids:
                        item, _ = await _verify_dictionary(
                            source, "bill_processes", authorization, entity_ids=bill_ids
                        )
                        report(item)
                    else:
                        report(SourceVerification("bill_processes", "no_validated_entity"))
            except Exception as error:  # noqa: BLE001 - no exception values or traces leave the probe
                report(SourceVerification("authorization", _category(error)))
    return tuple(reports)


def _emit(item: SourceVerification) -> None:
    print(json.dumps(asdict(item), ensure_ascii=False, separators=(",", ":")), flush=True)


def main(policy: VerificationPolicy) -> int:
    parser = argparse.ArgumentParser(
        description="Verify authorized TEST MES sources without raw data."
    )
    parser.add_argument("--test-environment", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 9, 1))
    parser.add_argument("--dataset", action="append", choices=sorted(_DATASETS))
    options = parser.parse_args()
    if not options.test_environment:
        _emit(SourceVerification("authorization", "test_environment_not_confirmed"))
        return 2
    from loguru import logger

    logger.remove()
    logging.disable(logging.CRITICAL)
    try:
        source = DotEnvSettingsSource(VerificationSettings, env_file=options.env_file)
        settings = VerificationSettings.model_validate(source())
        reports = asyncio.run(
            verify_sources(
                settings,
                options.date,
                policy=policy,
                emit=_emit,
                datasets=options.dataset,
            )
        )
    except Exception as error:  # noqa: BLE001 - configuration can contain secrets
        _emit(SourceVerification("authorization", _category(error)))
        return 1
    return 0 if all(item.category in {"validated", "validated_empty"} for item in reports) else 1
