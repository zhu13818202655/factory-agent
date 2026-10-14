import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.dictionary_schemas import (
    BillProcessResult,
    DictionaryResult,
    HangingLineResult,
    ProcessResult,
    ProductionTypeResult,
    StyleDirectoryResult,
    StyleFormResult,
    StyleProcessResult,
)
from factory_agent.data_api.hongzhao import MesRequest, MesResponse
from factory_agent.domain import DataScope, TenantId
from factory_agent.domain.errors import InvalidRequestError, UpstreamInvalidError
from factory_agent.ports.contracts import Clock
from factory_agent.ports.dictionary import DictionaryBatch, DictionaryCache, DictionaryValue

_SCHEMA_VERSION = 1
_SEMANTIC_VERSION = "safe-directory-v1"
_TTL = timedelta(hours=24)
_SHARED_SCOPE = "shared"
_FAMILY_BY_DATASET: dict[str, str] = {
    "styles": "style_directory",
    "style_types": "style_directory",
    "production_types": "production_types",
    "processes": "processes",
    "style_processes": "style_processes",
    "style_colors": "style_form",
    "style_sizes": "style_form",
    "bill_processes": "bill_processes",
    "hanging_lines": "hanging_lines",
}
_OPERATION_BY_FAMILY: dict[str, str] = {
    "style_directory": "HuohaoQuery",
    "production_types": "ScTypeQuery",
    "processes": "RfidWorktypeQuery",
    "style_processes": "HuohaoWorktypeQuery",
    "style_form": "HuohaoFormQuery",
    "bill_processes": "SclzdWorktypeQuery",
    "hanging_lines": "DgZuGridPageList",
}
_MODEL_BY_FAMILY: dict[str, type[DictionaryResult]] = {
    "style_directory": StyleDirectoryResult,
    "production_types": ProductionTypeResult,
    "processes": ProcessResult,
    "style_processes": StyleProcessResult,
    "style_form": StyleFormResult,
    "bill_processes": BillProcessResult,
    "hanging_lines": HangingLineResult,
}
_COLLECTIONS_BY_FAMILY: dict[str, dict[str, tuple[str, str]]] = {
    "style_directory": {
        "styles": ("huohaoList", "hh_total"),
        "style_types": ("huohaoTypeList", "ht_total"),
    },
    "style_form": {
        "styles": ("huohaoList", "hh_total"),
        "style_colors": ("huohaoColorList", "hc_total"),
        "style_sizes": ("huohaoChimaList", "hs_total"),
    },
    "production_types": {"production_types": ("sctypeList", "total")},
    "processes": {"processes": ("worktypeList", "total")},
    "style_processes": {"style_processes": ("list", "total")},
    "bill_processes": {"bill_processes": ("list", "total")},
    "hanging_lines": {"hanging_lines": ("list", "total")},
}
_FIELDS: dict[str, dict[str, type[str] | type[int] | type[bool]]] = {
    "styles": {
        "style_id": str,
        "style_code": str,
        "name": str,
        "type_id": str,
        "unit": str,
    },
    "style_types": {
        "type_id": str,
        "parent_type_id": str,
        "name": str,
        "deleted": bool,
    },
    "production_types": {
        "type_id": str,
        "name": str,
        "deleted": bool,
        "counts_cutting": bool,
        "counts_stock": bool,
    },
    "processes": {
        "process_id": str,
        "name": str,
        "type_name": str,
        "vehicle_type": str,
        "flow_type": int,
        "deleted": bool,
        "sort_order": str,
    },
    "style_processes": {
        "configuration_id": str,
        "style_id": str,
        "style_code": str,
        "process_id": str,
        "name": str,
        "type_id": str,
        "type_name": str,
        "sort_order": int,
        "active": bool,
        "last_process": bool,
    },
    "style_colors": {"configuration_id": str, "style_id": str, "color": str},
    "style_sizes": {
        "configuration_id": str,
        "style_id": str,
        "size": str,
        "fit": str,
        "weight": str,
    },
    "bill_processes": {
        "configuration_id": str,
        "bill_code": str,
        "style_id": str,
        "style_code": str,
        "process_id": str,
        "name": str,
        "type_id": str,
        "type_name": str,
        "sort_order": int,
        "last_process": bool,
    },
    "hanging_lines": {
        "configuration_id": str,
        "system_name": str,
        "line_code": str,
        "group_name": str,
    },
}
_KEY_FIELD: dict[str, str] = {
    "styles": "style_id",
    "style_types": "type_id",
    "production_types": "type_id",
    "processes": "process_id",
    "style_processes": "configuration_id",
    "style_colors": "configuration_id",
    "style_sizes": "configuration_id",
    "bill_processes": "configuration_id",
    "hanging_lines": "configuration_id",
}
_LOGICAL_FIELDS: dict[str, str] = {
    "bh": "identifier",
    "id": "configuration_id",
    "bbreed": "style_code",
    "stype": "type_id",
    "pbh": "parent_type_id",
    "description": "name",
    "dw": "unit",
    "huohao": "style_id",
    "huohaoname": "style_code",
    "wt": "process_id",
    "wtname": "name",
    "sctype": "type_id",
    "sctypename": "type_name",
    "sort": "sort_order",
    "using_state": "active",
    "zhgx": "last_process",
    "dh": "bill_code",
    "chima": "size",
    "banx": "fit",
    "kez": "weight",
    "color": "color",
    "name": "name",
    "isdelete": "deleted",
    "sfjcj": "counts_cutting",
    "sfcprk": "counts_stock",
    "gxtype": "flow_type",
    "section": "type_name",
    "vehicle_type": "vehicle_type",
    "wt_sort": "sort_order",
    "dgname": "system_name",
    "xianhao": "line_code",
    "zuBieName": "group_name",
    "total": "total",
    "hh_total": "total",
    "ht_total": "total",
    "hc_total": "total",
    "hs_total": "total",
}


class DictionaryValidationError(UpstreamInvalidError):
    """Only approved logical field identifiers leave a failed schema boundary."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__("directory response failed schema validation")
        self.fields = fields


def _validation_fields(family: str, error: ValidationError, payload: object) -> tuple[str, ...]:
    fields: set[str] = set()
    datasets = _COLLECTIONS_BY_FAMILY[family]
    for detail in error.errors(include_input=False, include_context=False, include_url=False):
        location = detail["loc"]
        dataset = (
            next(
                (key for key, (rows, total) in datasets.items() if location[0] in {rows, total}),
                family,
            )
            if location
            else family
        )
        name = _LOGICAL_FIELDS.get(str(location[-1]), "collection") if location else "collection"
        if name == "identifier":
            name = "style_id" if dataset in {"style_colors", "style_sizes"} else _KEY_FIELD[dataset]
        candidate: object = payload
        for path in location:
            if isinstance(candidate, dict) and isinstance(path, str):
                candidate = cast(dict[str, object], candidate).get(path)
            elif isinstance(candidate, (list, tuple)) and isinstance(path, int):
                values = cast(list[object] | tuple[object, ...], candidate)
                candidate = values[path] if path < len(values) else None
            else:
                candidate = None
                break
        kind = (
            "missing"
            if detail["type"] == "missing"
            else (
                "null"
                if candidate is None
                else "integer"
                if type(candidate) is int
                else "decimal"
                if type(candidate) is float
                else "boolean"
                if type(candidate) is bool
                else "string"
                if isinstance(candidate, str)
                else "invalid_shape"
            )
        )
        fields.add(f"{dataset}.{name}.{kind}")
    return tuple(sorted(fields))


class DictionaryAdapter(Protocol):
    @property
    def bundle(self) -> MesCredentialBundle: ...

    async def execute(self, request: MesRequest) -> MesResponse: ...


@dataclass(frozen=True, slots=True)
class _Collection:
    rows: tuple[Mapping[str, DictionaryValue], ...]
    total: int


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    collections: Mapping[str, _Collection]
    generated_at: datetime
    complete: bool
    reason: str | None = None
    cache_source: Literal["cache", "live"] = "live"


class MesDictionarySource:
    """Read approved tenant dictionaries through the shared directory cache.

    Price, employee, free-text remarks and authentication fields are excluded
    before cache encoding. Every named collection must prove completeness,
    and an entity cache never satisfies a whole-tenant directory request.
    The host supplies bill identifiers resolved from an authorized fact view.
    """

    def __init__(
        self,
        adapter: DictionaryAdapter,
        cache: DictionaryCache | None,
        clock: Clock,
        *,
        before_fetch: Callable[[], None] | None = None,
        max_entities: int = 20,
        max_rows: int = 250000,
        max_pages: int = 20,
        page_size: int = 200,
    ) -> None:
        if min(max_entities, max_rows, max_pages, page_size) < 1:
            raise ValueError("directory budgets must be positive")
        self._adapter = adapter
        self._cache = cache
        self._clock = clock
        self._before_fetch = before_fetch
        self._max_entities = max_entities
        self._max_rows = max_rows
        self._max_pages = max_pages
        self._page_size = page_size
        self._inflight: dict[tuple[TenantId, str, str], asyncio.Future[_DirectorySnapshot]] = {}

    async def fetch(
        self, dataset_id: str, scope: DataScope, entity_ids: tuple[str, ...] = ()
    ) -> DictionaryBatch:
        if self._adapter.bundle.app_key != str(scope.tenant_id):
            raise InvalidRequestError("directory scope does not match the active tenant")
        family = _FAMILY_BY_DATASET.get(dataset_id)
        if family is None:
            raise InvalidRequestError("directory dataset is not approved")
        entities = tuple(sorted(set(entity_ids)))
        if len(entities) > self._max_entities or any(
            not item or item != item.strip() or len(item) > 128 for item in entities
        ):
            raise InvalidRequestError("directory entity request is invalid or exceeds its budget")
        if family in {"style_form", "bill_processes"} and not entities:
            raise InvalidRequestError("directory dataset requires an authorized entity")
        if entities and family not in {"style_form", "bill_processes", "style_processes"}:
            raise InvalidRequestError("directory dataset does not accept entity filtering")

        keys = entities if family in {"style_form", "bill_processes"} else ("",)
        snapshots: list[_DirectorySnapshot] = []
        rows: list[Mapping[str, DictionaryValue]] = []
        total = 0
        for entity in keys:
            snapshot = await self._ensure_family(family, scope.tenant_id, entity)
            snapshots.append(snapshot)
            collection = snapshot.collections[dataset_id]
            selected = collection.rows
            if family == "style_processes" and entities:
                selected = tuple(row for row in selected if row["style_id"] in entities)
            total += len(selected) if snapshot.complete else collection.total
            rows.extend(selected)
            if len(rows) > self._max_rows:
                return DictionaryBatch(
                    dataset_id,
                    (),
                    total,
                    False,
                    snapshot.generated_at,
                    entity_ids=entities,
                    reason="row_budget_exhausted",
                )
            if not snapshot.complete:
                return DictionaryBatch(
                    dataset_id,
                    (),
                    total,
                    False,
                    snapshot.generated_at,
                    entity_ids=entities,
                    reason=snapshot.reason,
                )
        return DictionaryBatch(
            dataset_id=dataset_id,
            rows=tuple(rows),
            total=total,
            complete=True,
            generated_at=min(snapshot.generated_at for snapshot in snapshots),
            entity_ids=entities,
            cache_source="cache" if all(s.cache_source == "cache" for s in snapshots) else "live",
        )

    async def _ensure_family(
        self, family: str, tenant_id: TenantId, entity: str
    ) -> _DirectorySnapshot:
        key = (tenant_id, family, entity)
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[_DirectorySnapshot] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            snapshot = await self._load_family(family, tenant_id, entity)
            future.set_result(snapshot)
            return snapshot
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as error:
            future.set_exception(error)
            future.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    async def _load_family(
        self, family: str, tenant_id: TenantId, entity: str
    ) -> _DirectorySnapshot:
        if self._adapter.bundle.app_key != str(tenant_id):
            raise InvalidRequestError("directory scope does not match the active tenant")
        params: dict[str, object] = {
            "dataset": family,
            "entity": entity,
            "schema": _SCHEMA_VERSION,
            "semantics": _SEMANTIC_VERSION,
        }
        if self._cache is not None:
            lookup = await self._cache.get("identity_org", tenant_id, _SHARED_SCOPE, params)
            if lookup.value is not None:
                cached = self._decode(lookup.value, family, tenant_id, entity)
                if cached is not None:
                    return cached
        if family == "hanging_lines":
            snapshot = await self._fetch_hanging_lines(tenant_id)
        else:
            if self._before_fetch is not None:
                self._before_fetch()
            request_params: dict[str, object] = {}
            if family == "style_form":
                request_params["huohao"] = entity
            elif family == "bill_processes":
                request_params["dh"] = entity
            response = await self._adapter.execute(
                MesRequest(_OPERATION_BY_FAMILY[family], request_params)
            )
            if self._adapter.bundle.app_key != str(tenant_id):
                raise InvalidRequestError("directory tenant binding changed during its request")
            oversized = self._check_raw_budget(family, response.result)
            if oversized is not None:
                return oversized
            try:
                model = _MODEL_BY_FAMILY[family].from_payload(response.result)
            except ValidationError as error:
                raise DictionaryValidationError(
                    _validation_fields(family, error, response.result)
                ) from None
            except ValueError:
                raise UpstreamInvalidError("directory response failed schema validation") from None
            snapshot = self._project(model, entity)
        if snapshot.complete and self._cache is not None:
            await self._cache.put(
                "identity_org",
                tenant_id,
                _SHARED_SCOPE,
                params,
                self._encode(snapshot, family, tenant_id, entity),
                ttl_seconds=int(_TTL.total_seconds()),
            )
        return snapshot

    async def _fetch_hanging_lines(self, tenant_id: TenantId) -> _DirectorySnapshot:
        rows: list[Mapping[str, DictionaryValue]] = []
        seen: set[str] = set()
        expected_total: int | None = None
        for page in range(1, self._max_pages + 1):
            if self._adapter.bundle.app_key != str(tenant_id):
                raise InvalidRequestError("directory scope does not match the active tenant")
            if self._before_fetch is not None:
                self._before_fetch()
            response = await self._adapter.execute(
                MesRequest("DgZuGridPageList", {"page": page, "size": self._page_size})
            )
            if self._adapter.bundle.app_key != str(tenant_id):
                raise InvalidRequestError("directory tenant binding changed during its request")
            oversized = self._check_raw_budget("hanging_lines", response.result)
            if oversized is not None:
                return oversized
            try:
                result = HangingLineResult.model_validate(response.result)
            except ValidationError as error:
                raise DictionaryValidationError(
                    _validation_fields("hanging_lines", error, response.result)
                ) from None
            except ValueError:
                raise UpstreamInvalidError("directory response failed schema validation") from None
            if expected_total is None:
                expected_total = result.total
            elif result.total != expected_total:
                return self._incomplete_hanging(result.total, "total_drift")
            if expected_total > self._max_rows:
                return self._incomplete_hanging(expected_total, "row_budget_exhausted")
            for row in result.list:
                if row.id in seen:
                    return self._incomplete_hanging(expected_total, "duplicate_page")
                seen.add(row.id)
                rows.append(
                    {
                        "configuration_id": row.id,
                        "system_name": row.dgname,
                        "line_code": row.xianhao,
                        "group_name": row.zuBieName,
                    }
                )
            if len(rows) > expected_total:
                return self._incomplete_hanging(expected_total, "row_overflow")
            if len(rows) == expected_total:
                collections = {"hanging_lines": _Collection(tuple(rows), expected_total)}
                _validate_relations(collections, "")
                return _DirectorySnapshot(collections, self._clock.now(), True)
            if not result.list:
                return self._incomplete_hanging(expected_total, "missing_pages")
        return self._incomplete_hanging(expected_total or 0, "page_budget_exhausted")

    def _incomplete_hanging(self, total: int, reason: str) -> _DirectorySnapshot:
        return _DirectorySnapshot(
            {"hanging_lines": _Collection((), total)}, self._clock.now(), False, reason
        )

    def _check_raw_budget(self, family: str, payload: object) -> _DirectorySnapshot | None:
        if not isinstance(payload, dict):
            return None
        values = cast(dict[str, object], payload)
        count = 0
        collections: dict[str, _Collection] = {}
        for dataset, (row_field, total_field) in _COLLECTIONS_BY_FAMILY[family].items():
            raw_rows = values.get(row_field)
            total = values.get(total_field)
            if not isinstance(raw_rows, (list, tuple)) or type(total) is not int or total < 0:
                return None
            count += len(cast(list[object] | tuple[object, ...], raw_rows))
            collections[dataset] = _Collection((), total)
        if count > self._max_rows:
            return _DirectorySnapshot(collections, self._clock.now(), False, "row_budget_exhausted")
        return None

    def _project(self, model: DictionaryResult, entity: str) -> _DirectorySnapshot:
        collections: dict[str, _Collection] = {}
        if isinstance(model, (StyleDirectoryResult, StyleFormResult)):
            collections["styles"] = _Collection(
                tuple(
                    {
                        "style_id": r.bh,
                        "style_code": r.bbreed,
                        "name": r.description,
                        "type_id": r.stype,
                        "unit": r.dw,
                    }
                    for r in model.huohaoList
                ),
                model.hh_total,
            )
        if isinstance(model, StyleDirectoryResult):
            collections["style_types"] = _Collection(
                tuple(
                    {
                        "type_id": r.bh,
                        "parent_type_id": r.pbh,
                        "name": r.name,
                        "deleted": bool(r.isdelete),
                    }
                    for r in model.huohaoTypeList
                ),
                model.ht_total,
            )
        elif isinstance(model, StyleFormResult):
            collections["style_colors"] = _Collection(
                tuple(
                    {
                        "configuration_id": r.id,
                        "style_id": r.bh,
                        "color": r.color,
                    }
                    for r in model.huohaoColorList
                ),
                model.hc_total,
            )
            collections["style_sizes"] = _Collection(
                tuple(
                    {
                        "configuration_id": r.id,
                        "style_id": r.bh,
                        "size": r.chima,
                        "fit": r.banx,
                        "weight": r.kez,
                    }
                    for r in model.huohaoChimaList
                ),
                model.hs_total,
            )
        elif isinstance(model, ProductionTypeResult):
            collections["production_types"] = _Collection(
                tuple(
                    {
                        "type_id": r.bh,
                        "name": r.name,
                        "deleted": bool(r.isdelete),
                        "counts_cutting": bool(r.sfjcj),
                        "counts_stock": bool(r.sfcprk),
                    }
                    for r in model.sctypeList
                ),
                model.total,
            )
        elif isinstance(model, ProcessResult):
            collections["processes"] = _Collection(
                tuple(
                    {
                        "process_id": r.bh,
                        "name": r.name,
                        "type_name": r.section,
                        "vehicle_type": r.vehicle_type,
                        "flow_type": r.gxtype,
                        "deleted": bool(r.isdelete),
                        "sort_order": r.wt_sort,
                    }
                    for r in model.worktypeList
                ),
                model.total,
            )
        elif isinstance(model, StyleProcessResult):
            collections["style_processes"] = _Collection(
                tuple(
                    {
                        "configuration_id": r.id,
                        "style_id": r.huohao,
                        "style_code": r.huohaoname,
                        "process_id": r.wt,
                        "name": r.wtname,
                        "type_id": r.sctype,
                        "type_name": r.sctypename,
                        "sort_order": r.sort,
                        "active": bool(r.using_state),
                        "last_process": bool(r.zhgx),
                    }
                    for r in model.list
                ),
                model.total,
            )
        elif isinstance(model, BillProcessResult):
            collections["bill_processes"] = _Collection(
                tuple(
                    {
                        "configuration_id": r.id,
                        "bill_code": r.dh,
                        "style_id": r.huohao,
                        "style_code": r.huohaoname,
                        "process_id": r.wt,
                        "name": r.wtname,
                        "type_id": r.sctype,
                        "type_name": r.sctypename,
                        "sort_order": r.sort,
                        "last_process": bool(r.zhgx),
                    }
                    for r in model.list
                ),
                model.total,
            )
        if any(len(c.rows) != c.total for c in collections.values()):
            return _DirectorySnapshot(
                collections, self._clock.now(), False, "collection_incomplete"
            )
        if sum(c.total for c in collections.values()) > self._max_rows:
            return _DirectorySnapshot(collections, self._clock.now(), False, "row_budget_exhausted")
        _validate_relations(collections, entity)
        return _DirectorySnapshot(collections, self._clock.now(), True)

    def _encode(
        self, snapshot: _DirectorySnapshot, family: str, tenant_id: TenantId, entity: str
    ) -> bytes:
        value = {
            "version": _SCHEMA_VERSION,
            "semantics": _SEMANTIC_VERSION,
            "family": family,
            "tenant": _tenant_hash(tenant_id),
            "entity": entity,
            "generated_at": snapshot.generated_at.isoformat(),
            "collections": {
                key: {"rows": [dict(r) for r in c.rows], "total": c.total}
                for key, c in snapshot.collections.items()
            },
        }
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _decode(
        self, raw: bytes, family: str, tenant_id: TenantId, entity: str
    ) -> _DirectorySnapshot | None:
        try:
            value = cast(dict[str, object], json.loads(raw.decode("utf-8")))
            if set(value) != {
                "version",
                "semantics",
                "family",
                "tenant",
                "entity",
                "generated_at",
                "collections",
            }:
                return None
            if (
                value["version"] != _SCHEMA_VERSION
                or value["semantics"] != _SEMANTIC_VERSION
                or value["family"] != family
                or value["tenant"] != _tenant_hash(tenant_id)
                or value["entity"] != entity
            ):
                return None
            generated_at = datetime.fromisoformat(cast(str, value["generated_at"]))
            if generated_at.tzinfo is None:
                return None
            age = self._clock.now() - generated_at
            if age < timedelta(0) or age >= _TTL:
                return None
            raw_collections = cast(dict[str, dict[str, object]], value["collections"])
            expected = set(_COLLECTIONS_BY_FAMILY[family])
            if set(raw_collections) != expected:
                return None
            collections: dict[str, _Collection] = {}
            for dataset, collection in raw_collections.items():
                if set(collection) != {"rows", "total"}:
                    return None
                total = collection["total"]
                rows = collection["rows"]
                if type(total) is not int or not isinstance(rows, list):
                    return None
                entries = cast(list[object], rows)
                if total != len(entries):
                    return None
                typed_rows: list[Mapping[str, DictionaryValue]] = []
                for row in entries:
                    if not isinstance(row, dict):
                        return None
                    fields = _FIELDS[dataset]
                    typed_row = cast(dict[str, DictionaryValue], row)
                    if set(typed_row) != set(fields) or any(
                        type(typed_row[field]) is not kind for field, kind in fields.items()
                    ):
                        return None
                    typed_rows.append(typed_row)
                collections[dataset] = _Collection(tuple(typed_rows), total)
            if sum(c.total for c in collections.values()) > self._max_rows:
                return None
            _validate_relations(collections, entity)
            return _DirectorySnapshot(collections, generated_at, True, cache_source="cache")
        except (ValueError, KeyError, TypeError, AttributeError, UpstreamInvalidError):
            return None


def _validate_relations(collections: Mapping[str, _Collection], entity: str) -> None:
    for dataset, collection in collections.items():
        keys = [row[_KEY_FIELD[dataset]] for row in collection.rows]
        if len(keys) != len(set(keys)):
            raise UpstreamInvalidError("directory contains duplicate stable identifiers")
        for row in collection.rows:
            for name, value in row.items():
                if (name.endswith("_id") or name in {"style_code", "bill_code", "line_code"}) and (
                    not isinstance(value, str) or not value or value != value.strip()
                ):
                    raise UpstreamInvalidError("directory contains an invalid stable relation")
            if entity:
                entity_field = "bill_code" if dataset == "bill_processes" else "style_id"
                if row.get(entity_field) != entity:
                    raise UpstreamInvalidError("directory response does not match its entity")
        if dataset in {"style_processes", "bill_processes"}:
            for code_field, name_field in (
                ("style_id", "style_code"),
                ("process_id", "name"),
                ("type_id", "type_name"),
            ):
                names_by_code: dict[DictionaryValue, DictionaryValue] = {}
                for row in collection.rows:
                    code, name = row[code_field], row[name_field]
                    if code in names_by_code and names_by_code[code] != name:
                        raise UpstreamInvalidError("directory code has conflicting names")
                    names_by_code[code] = name
        if dataset == "bill_processes" and len({r["style_id"] for r in collection.rows}) > 1:
            raise UpstreamInvalidError("bill configuration has conflicting style relations")
    styles = collections.get("styles")
    if styles is not None:
        codes = [row["style_code"] for row in styles.rows]
        if len(codes) != len(set(codes)):
            raise UpstreamInvalidError("style codes do not identify a unique style")
        types = collections.get("style_types")
        if types is not None:
            type_ids = {row["type_id"] for row in types.rows}
            if any(row["type_id"] not in type_ids for row in styles.rows):
                raise UpstreamInvalidError("style references an unknown type")
        elif entity:
            style_ids = {row["style_id"] for row in styles.rows}
            if any(
                row["style_id"] not in style_ids
                for dataset in ("style_colors", "style_sizes")
                for row in collections[dataset].rows
            ):
                raise UpstreamInvalidError("style configuration references an unknown style")


def _tenant_hash(tenant_id: TenantId) -> str:
    return hashlib.sha256(str(tenant_id).encode()).hexdigest()
