import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
import yaml

from factory_agent.application.cache import AuthAwareCache
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import MesRequest, MesResponse
from factory_agent.data_api.knowledge_directory import MesDictionarySource
from factory_agent.domain import DataScope, DeptId, EmployeeId, ScopeVersion, TenantId, UserId
from factory_agent.domain.errors import InvalidRequestError, UpstreamInvalidError
from tests.support.cache import InMemoryCacheStore

NOW = datetime(2026, 10, 13, 2, tzinfo=UTC)
TENANT = TenantId("synthetic-factory-a")


@dataclass
class _Clock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


def _scope(tenant: TenantId = TENANT) -> DataScope:
    return DataScope(
        tenant_id=tenant,
        employee_ids=frozenset({EmployeeId("synthetic-worker")}),
        dept_ids=frozenset({DeptId("synthetic-group")}),
        evaluated_at=NOW,
        scope_version=ScopeVersion("synthetic-scope-v1"),
        mes_filtered=False,
    )


class _Adapter:
    def __init__(
        self,
        responses: dict[str, object],
        *,
        role: str = "00",
        tenant: TenantId = TENANT,
        handler: Callable[[MesRequest], object] | None = None,
    ) -> None:
        self.responses = responses
        self.calls: list[MesRequest] = []
        self.handler = handler
        self._bundle = MesCredentialBundle(
            access_token="synthetic-token",
            app_key=str(tenant),
            sign="synthetic-sign",
            timestamp=1,
            expires_at=datetime.max.replace(tzinfo=UTC),
            user=UserId("synthetic-worker"),
            uname="synthetic-name",
            roles=(role,),
        )

    @property
    def bundle(self) -> MesCredentialBundle:
        return self._bundle

    def change_tenant(self, tenant: str) -> None:
        self._bundle = replace(self._bundle, app_key=tenant)

    async def execute(self, request: MesRequest) -> MesResponse:
        self.calls.append(request)
        await asyncio.sleep(0)
        result = self.handler(request) if self.handler else self.responses[request.operation_id]
        return MesResponse(result=result, footer=None)


def _style(style_id: str = "0001", style_code: str = "STYLE-A") -> dict[str, object]:
    return {
        "bh": style_id,
        "bbreed": style_code,
        "description": "synthetic garment",
        "stype": "TYPE-A",
        "dw": "件",
        "remark": "PRIVATE_REMARK_CANARY",
    }


def style_directory_payload() -> dict[str, object]:
    return {
        "huohaoList": [_style()],
        "hh_total": 1,
        "huohaoTypeList": [{"bh": "TYPE-A", "pbh": "#", "name": "archive bucket", "isdelete": 0}],
        "ht_total": 1,
    }


def style_form_payload(style_id: str = "0001") -> dict[str, object]:
    return {
        "huohaoList": [_style(style_id, "STYLE-" + style_id)],
        "hh_total": 1,
        "huohaoColorList": [
            {"id": 1, "bh": style_id, "color": "synthetic blue", "uploadguid": "PRIVATE_GUID"}
        ],
        "hc_total": 1,
        "huohaoChimaList": [
            {
                "id": 2,
                "bh": style_id,
                "chima": "XXL",
                "banx": "fit-a",
                "kez": None,
                "price": "PRIVATE_PRICE_CANARY",
                "xs_price": "PRIVATE_SALE_CANARY",
            },
            {"id": 3, "bh": style_id, "chima": "2XL", "banx": "fit-b", "kez": "180"},
            {"id": 4, "bh": style_id, "chima": "XXL", "banx": "fit-b", "kez": "180"},
        ],
        "hs_total": 3,
    }


def production_type_payload() -> dict[str, object]:
    return {
        "sctypeList": [
            {"bh": "PT-A", "name": "synthetic stage", "isdelete": 0, "sfjcj": 1, "sfcprk": 0}
        ],
        "total": 1,
    }


def style_process_payload(style_id: str = "0001", config: int = 10) -> dict[str, object]:
    return {
        "id": config,
        "huohao": style_id,
        "huohaoname": "STYLE-" + style_id,
        "wt": "PROCESS-A",
        "wtname": "synthetic sewing",
        "sort": 1,
        "sctype": "PT-A",
        "sctypename": "synthetic stage",
        "using_state": 1,
        "zhgx": 0,
        "theoretical_work_hours": "PRIVATE_HOURS_CANARY",
    }


_style_directory = style_directory_payload
_form = style_form_payload
_type_directory = production_type_payload
_process = style_process_payload


def _source(
    adapter: _Adapter,
    *,
    store: InMemoryCacheStore | None = None,
    clock: _Clock | None = None,
    before_fetch: Callable[[], None] | None = None,
    max_entities: int = 20,
    max_rows: int = 250000,
) -> tuple[MesDictionarySource, InMemoryCacheStore]:
    storage = store or InMemoryCacheStore()
    cache = AuthAwareCache(
        storage,
        contract_version="synthetic-contract-v1",
        metric_version="synthetic-metric-v1",
        data_version="synthetic-data-v1",
    )
    return MesDictionarySource(
        adapter,
        cache,
        clock or _Clock(),
        before_fetch=before_fetch,
        max_entities=max_entities,
        max_rows=max_rows,
    ), storage


@pytest.mark.asyncio
async def test_named_style_collections_share_one_validated_cache_snapshot() -> None:
    adapter = _Adapter({"HuohaoQuery": _style_directory()})
    source, store = _source(adapter)
    styles = await source.fetch("styles", _scope())
    types = await source.fetch("style_types", _scope())

    assert styles.complete and types.complete
    assert styles.rows[0]["style_id"] == "0001"
    assert types.rows[0]["type_id"] == "TYPE-A"
    assert styles.cache_source == "live" and types.cache_source == "cache"
    assert len(adapter.calls) == 1
    assert set(store.ttl.values()) == {86400}
    assert "PRIVATE_REMARK_CANARY" not in repr(store.values)
    assert str(TENANT) not in next(iter(store.values))
    with pytest.raises(TypeError):
        cast(dict[str, object], styles.rows[0])["style_id"] = "changed"


@pytest.mark.asyncio
async def test_nullable_descriptive_style_metadata_stays_unknown_without_fabricating_labels() -> (
    None
):
    payload = _style_directory()
    rows = cast(list[dict[str, object]], payload["huohaoList"])
    rows[0]["description"] = None
    rows[0]["dw"] = None
    source, _ = _source(_Adapter({"HuohaoQuery": payload}))
    result = await source.fetch("styles", _scope())
    assert result.complete and result.total == 1
    assert result.rows[0]["style_id"] == "0001" and result.rows[0]["style_code"] == "STYLE-A"
    assert result.rows[0]["name"] == result.rows[0]["unit"] == ""
    rows[0]["bh"] = None
    invalid, _ = _source(_Adapter({"HuohaoQuery": payload}))
    with pytest.raises(UpstreamInvalidError):
        await invalid.fetch("styles", _scope())


@pytest.mark.asyncio
async def test_integral_numeric_configuration_id_uses_exact_canonical_string() -> None:
    adapter = _Adapter(
        {
            "HuohaoWorktypeQuery": {
                "list": [_process() | {"id": 10.0}],
                "total": 1,
            }
        }
    )
    source, _ = _source(adapter)
    result = await source.fetch("style_processes", _scope())
    assert result.complete and result.rows[0]["configuration_id"] == "10"
    for invalid_id in (10.25, float("inf"), float(2**53 + 2), True):
        invalid, store = _source(
            _Adapter(
                {
                    "HuohaoWorktypeQuery": {
                        "list": [_process() | {"id": invalid_id}],
                        "total": 1,
                    }
                }
            )
        )
        with pytest.raises(UpstreamInvalidError):
            await invalid.fetch("style_processes", _scope())
        assert store.values == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["00", "01", "02", "99"])
async def test_all_current_roles_read_same_safe_tenant_dictionary(role: str) -> None:
    adapter = _Adapter({"ScTypeQuery": _type_directory()}, role=role)
    source, _ = _source(adapter)
    batch = await source.fetch("production_types", _scope())

    assert batch.complete and batch.total == 1
    assert batch.rows == (
        {
            "type_id": "PT-A",
            "name": "synthetic stage",
            "deleted": False,
            "counts_cutting": True,
            "counts_stock": False,
        },
    )
    assert adapter.calls[0].params == {}


@pytest.mark.asyncio
async def test_form_proves_colors_and_sizes_and_preserves_duplicate_size_records() -> None:
    adapter = _Adapter({"HuohaoFormQuery": _form()})
    source, store = _source(adapter)
    sizes = await source.fetch("style_sizes", _scope(), ("0001",))
    colors = await source.fetch("style_colors", _scope(), ("0001",))

    assert sizes.complete and sizes.total == 3
    assert {row["size"] for row in sizes.rows} == {"XXL", "2XL"}
    assert {row["fit"] for row in sizes.rows} == {"fit-a", "fit-b"}
    assert colors.complete and colors.total == 1
    assert sizes.entity_ids == ("0001",)
    assert len(adapter.calls) == 1
    assert adapter.calls[0].params == {"huohao": "0001"}
    for canary in ("PRIVATE_PRICE_CANARY", "PRIVATE_SALE_CANARY", "PRIVATE_GUID"):
        assert canary not in repr(store.values)
        assert canary not in repr(sizes)


@pytest.mark.asyncio
@pytest.mark.parametrize("total_field", ["hh_total", "hc_total", "hs_total"])
async def test_any_incomplete_form_collection_prevents_answers_and_cache(total_field: str) -> None:
    payload = _form()
    payload[total_field] = 99
    adapter = _Adapter({"HuohaoFormQuery": payload})
    source, store = _source(adapter)

    result = await source.fetch("style_sizes", _scope(), ("0001",))

    assert result.complete is False
    assert result.rows == ()
    assert result.reason == "collection_incomplete"
    assert store.values == {}


@pytest.mark.asyncio
async def test_entity_forms_are_separate_and_shared_only_for_matching_entity() -> None:
    adapter = _Adapter({}, handler=lambda request: _form(str(request.params["huohao"])))
    source, store = _source(adapter)

    first = await source.fetch("style_sizes", _scope(), ("0001",))
    second = await source.fetch("style_sizes", _scope(), ("0002",))
    merged = await source.fetch("style_colors", _scope(), ("0002", "0001"))

    assert first.rows[0]["style_id"] == "0001"
    assert second.rows[0]["style_id"] == "0002"
    assert merged.total == 2 and merged.cache_source == "cache"
    assert len(adapter.calls) == 2 and len(store.values) == 2
    assert merged.entity_ids == ("0001", "0002")


@pytest.mark.asyncio
async def test_full_style_process_relations_support_subsets_without_extra_requests() -> None:
    adapter = _Adapter(
        {
            "HuohaoWorktypeQuery": {
                "list": [
                    _process(),
                    _process("0002", 11),
                    {**_process(config=12), "using_state": 0},
                ],
                "total": 3,
            }
        }
    )
    source, _ = _source(adapter)

    subset = await source.fetch("style_processes", _scope(), ("0001",))
    full = await source.fetch("style_processes", _scope())

    assert subset.total == 2 and full.total == 3
    assert {r["active"] for r in subset.rows} == {True, False}
    assert len({r["process_id"] for r in subset.rows}) == 1
    assert len(adapter.calls) == 1 and adapter.calls[0].params == {}
    assert full.entity_ids == () and full.cache_source == "cache"


@pytest.mark.asyncio
async def test_process_directory_drops_price_and_keeps_vehicle_distinct() -> None:
    adapter = _Adapter(
        {
            "RfidWorktypeQuery": {
                "worktypeList": [
                    {
                        "bh": "P-A",
                        "name": "synthetic sewing",
                        "gxtype": 0,
                        "isdelete": 0,
                        "section": "synthetic stage",
                        "vehicle_type": None,
                        "wt_sort": None,
                        "default_price": "PRIVATE_PRICE_CANARY",
                        "xz_price": "PRIVATE_LIMIT_CANARY",
                        "movepassword": "PRIVATE_SECRET",
                    }
                ],
                "total": 1,
            }
        }
    )
    source, store = _source(adapter)

    result = await source.fetch("processes", _scope())

    assert result.rows[0]["vehicle_type"] == ""
    assert result.rows[0]["name"] == "synthetic sewing"
    assert "PRIVATE_" not in repr(result) and "PRIVATE_" not in repr(store.values)


@pytest.mark.asyncio
async def test_bill_configuration_retains_bill_and_checks_response_entity() -> None:
    adapter = _Adapter(
        {"SclzdWorktypeQuery": {"list": [{**_process(), "dh": "BILL-A"}], "total": 1}}
    )
    source, _ = _source(adapter)

    result = await source.fetch("bill_processes", _scope(), ("BILL-A",))

    assert result.complete and result.rows[0]["bill_code"] == "BILL-A"
    assert adapter.calls[0].params == {"dh": "BILL-A"}
    with pytest.raises(UpstreamInvalidError, match="does not match its entity"):
        await source.fetch("bill_processes", _scope(), ("BILL-B",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dataset,entities",
    [
        ("unknown", ()),
        ("style_sizes", ()),
        ("bill_processes", ()),
        ("styles", ("0001",)),
        ("style_colors", (" 0001",)),
    ],
)
async def test_invalid_dataset_or_entity_requests_perform_zero_source_calls(
    dataset: str, entities: tuple[str, ...]
) -> None:
    adapter = _Adapter({})
    source, store = _source(adapter)

    with pytest.raises(InvalidRequestError):
        await source.fetch(dataset, _scope(), entities)
    assert adapter.calls == [] and store.values == {}


@pytest.mark.asyncio
async def test_cross_tenant_scope_is_rejected_before_cache_or_source() -> None:
    adapter = _Adapter({"ScTypeQuery": _type_directory()})
    source, store = _source(adapter)
    await source.fetch("production_types", _scope())

    with pytest.raises(InvalidRequestError, match="active tenant"):
        await source.fetch("production_types", _scope(TenantId("synthetic-factory-b")))
    assert len(adapter.calls) == 1 and len(store.values) == 1


@pytest.mark.asyncio
async def test_distinct_tenant_sources_never_reuse_each_others_cache() -> None:
    store = InMemoryCacheStore()
    adapter_a = _Adapter({"ScTypeQuery": _type_directory()})
    adapter_b = _Adapter(
        {"ScTypeQuery": {"sctypeList": [], "total": 0}}, tenant=TenantId("synthetic-factory-b")
    )
    source_a, _ = _source(adapter_a, store=store)
    source_b, _ = _source(adapter_b, store=store)

    a = await source_a.fetch("production_types", _scope())
    b = await source_b.fetch("production_types", _scope(TenantId("synthetic-factory-b")))

    assert a.total == 1 and b.total == 0
    assert len(adapter_a.calls) == len(adapter_b.calls) == 1
    assert len(store.values) == 2


@pytest.mark.asyncio
async def test_empty_complete_collection_is_reused_and_concurrent_ensure_is_coalesced() -> None:
    adapter = _Adapter({"ScTypeQuery": {"sctypeList": [], "total": 0}})
    source, _ = _source(adapter)

    batches = await asyncio.gather(*(source.fetch("production_types", _scope()) for _ in range(5)))

    assert all(batch.complete and batch.rows == () and batch.total == 0 for batch in batches)
    assert len(adapter.calls) == 1
    assert all(batch.cache_source == "live" for batch in batches)


@pytest.mark.asyncio
async def test_no_persistent_cache_uses_live_data_and_merges_concurrent_requests() -> None:
    adapter = _Adapter({"ScTypeQuery": _type_directory()})
    source = MesDictionarySource(adapter, None, _Clock())
    batches = await asyncio.gather(*(source.fetch("production_types", _scope()) for _ in range(4)))
    assert all(batch.complete and batch.cache_source == "live" for batch in batches)
    assert len(adapter.calls) == 1
    later = await source.fetch("production_types", _scope())
    assert later.complete and len(adapter.calls) == 2


def hanging_line_payload(identifier: int) -> dict[str, object]:
    return {
        "id": identifier,
        "dgname": "synthetic system",
        "xianhao": str(identifier),
        "zuBieName": "synthetic group",
        "dg_Pwd": "PRIVATE_HANGING_CANARY",
    }


_hanging_line = hanging_line_payload


@pytest.mark.asyncio
async def test_hanging_line_pages_are_bounded_counted_and_shared_without_department_claim() -> None:
    pages = {1: [_hanging_line(1), _hanging_line(2)], 2: [_hanging_line(3)]}
    adapter = _Adapter(
        {},
        handler=lambda request: {
            "list": pages[int(request.params["page"])],
            "total": 3,
        },
    )
    charges: list[None] = []
    source, store = _source(adapter, before_fetch=lambda: charges.append(None))

    result = await source.fetch("hanging_lines", _scope())
    cached = await source.fetch("hanging_lines", _scope())

    assert result.complete and result.total == 3
    assert result.rows[0] == {
        "configuration_id": "1",
        "system_name": "synthetic system",
        "line_code": "1",
        "group_name": "synthetic group",
    }
    assert len(adapter.calls) == 2 and charges == [None, None]
    assert cached.cache_source == "cache"
    assert [r.params for r in adapter.calls] == [{"page": 1, "size": 200}, {"page": 2, "size": 200}]
    assert "PRIVATE_HANGING_CANARY" not in repr(store.values)
    assert "department" not in result.rows[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,reason",
    [
        ("drift", "total_drift"),
        ("duplicate", "duplicate_page"),
        ("empty", "missing_pages"),
    ],
)
async def test_hanging_line_pagination_anomalies_never_cache_partial_rows(
    mode: str, reason: str
) -> None:
    def page(request: MesRequest) -> object:
        if request.params["page"] == 1:
            return {"list": [_hanging_line(1)], "total": 3}
        return {
            "list": [] if mode == "empty" else [_hanging_line(1 if mode == "duplicate" else 2)],
            "total": 4 if mode == "drift" else 3,
        }

    adapter = _Adapter({}, handler=page)
    source, store = _source(adapter)
    result = await source.fetch("hanging_lines", _scope())
    assert result.complete is False and result.reason == reason and result.rows == ()
    assert store.values == {} and len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_hanging_line_page_and_row_budgets_stop_actual_requests() -> None:
    adapter = _Adapter(
        {},
        handler=lambda request: {
            "list": [_hanging_line(int(request.params["page"]))],
            "total": 3,
        },
    )
    source = MesDictionarySource(adapter, None, _Clock(), max_pages=1)
    result = await source.fetch("hanging_lines", _scope())
    assert result.complete is False and result.reason == "page_budget_exhausted"
    assert len(adapter.calls) == 1
    limited = MesDictionarySource(adapter, None, _Clock(), max_rows=2)
    result = await limited.fetch("hanging_lines", _scope())
    assert result.complete is False and result.reason == "row_budget_exhausted"
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_cache_corruption_expiry_and_unknown_version_require_fresh_validation() -> None:
    clock = _Clock()
    adapter = _Adapter({"ScTypeQuery": _type_directory()})
    source, store = _source(adapter, clock=clock)
    await source.fetch("production_types", _scope())
    key = next(iter(store.values))
    store.values[key] = b"not-json"
    assert (await source.fetch("production_types", _scope())).cache_source == "live"

    value = cast(dict[str, object], json.loads(store.values[key]))
    value["version"] = 999
    store.values[key] = json.dumps(value).encode()
    assert (await source.fetch("production_types", _scope())).cache_source == "live"

    clock.value += timedelta(hours=24)
    assert (await source.fetch("production_types", _scope())).cache_source == "live"
    assert len(adapter.calls) == 4


@pytest.mark.asyncio
async def test_cached_unsafe_extra_fields_are_rejected_and_never_returned() -> None:
    adapter = _Adapter({"ScTypeQuery": _type_directory()})
    source, store = _source(adapter)
    await source.fetch("production_types", _scope())
    key = next(iter(store.values))
    value = cast(dict[str, object], json.loads(store.values[key]))
    collections = cast(dict[str, dict[str, object]], value["collections"])
    rows = cast(list[dict[str, object]], collections["production_types"]["rows"])
    rows[0]["password"] = "PRIVATE_CACHE_CANARY"
    store.values[key] = json.dumps(value).encode()

    result = await source.fetch("production_types", _scope())

    assert result.cache_source == "live" and len(adapter.calls) == 2
    assert "PRIVATE_CACHE_CANARY" not in repr(result)
    assert "PRIVATE_CACHE_CANARY" not in repr(store.values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_process(), _process()],
        [_process() | {"wt": ""}],
        [_process() | {"sctype": ""}],
    ],
)
async def test_invalid_or_duplicate_stable_identifiers_fail_closed(
    rows: list[dict[str, object]],
) -> None:
    adapter = _Adapter({"HuohaoWorktypeQuery": {"list": rows, "total": len(rows)}})
    source, store = _source(adapter)

    with pytest.raises(UpstreamInvalidError):
        await source.fetch("style_processes", _scope())
    assert store.values == {}


@pytest.mark.asyncio
async def test_unknown_style_type_relationship_is_rejected() -> None:
    payload = _style_directory()
    payload["huohaoTypeList"] = []
    payload["ht_total"] = 0
    source, store = _source(_Adapter({"HuohaoQuery": payload}))

    with pytest.raises(UpstreamInvalidError, match="unknown type"):
        await source.fetch("styles", _scope())
    assert store.values == {}


@pytest.mark.asyncio
async def test_conflicting_process_labels_fail_without_caching_a_guessed_relation() -> None:
    adapter = _Adapter(
        {
            "HuohaoWorktypeQuery": {
                "list": [_process(), _process(config=11) | {"wtname": "conflicting label"}],
                "total": 2,
            }
        }
    )
    source, store = _source(adapter)
    with pytest.raises(UpstreamInvalidError, match="conflicting names"):
        await source.fetch("style_processes", _scope())
    assert store.values == {}


@pytest.mark.asyncio
async def test_changed_tenant_binding_during_request_cannot_populate_previous_tenant_cache() -> (
    None
):
    adapter = _Adapter({})

    def change_binding(request: MesRequest) -> object:
        assert request.operation_id == "ScTypeQuery"
        adapter.change_tenant("synthetic-factory-b")
        return _type_directory()

    adapter.handler = change_binding
    source, store = _source(adapter)
    with pytest.raises(InvalidRequestError, match="binding changed"):
        await source.fetch("production_types", _scope())
    assert len(adapter.calls) == 1 and store.values == {}


@pytest.mark.asyncio
async def test_source_request_guard_charges_only_real_calls_and_can_deny_before_call() -> None:
    charges: list[None] = []
    adapter = _Adapter({"ScTypeQuery": _type_directory()})
    source, _ = _source(adapter, before_fetch=lambda: charges.append(None))
    await source.fetch("production_types", _scope())
    await source.fetch("production_types", _scope())
    assert charges == [None]

    def deny() -> None:
        raise RuntimeError("synthetic shared budget exhausted")

    denied_adapter = _Adapter({})
    denied, _ = _source(denied_adapter, before_fetch=deny)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await denied.fetch("production_types", _scope())
    assert denied_adapter.calls == []


@pytest.mark.asyncio
async def test_row_and_entity_budgets_are_bounded_and_incomplete_rows_are_not_cached() -> None:
    adapter = _Adapter({"HuohaoFormQuery": _form()})
    source, store = _source(adapter, max_rows=4, max_entities=1)
    result = await source.fetch("style_sizes", _scope(), ("0001",))
    assert result.complete is False and result.reason == "row_budget_exhausted"
    assert result.rows == () and store.values == {}
    with pytest.raises(InvalidRequestError, match="budget"):
        await source.fetch("style_sizes", _scope(), ("0001", "0002"))
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_cache_unavailable_uses_live_validated_directory() -> None:
    adapter = _Adapter({"ScTypeQuery": _type_directory()})
    store = InMemoryCacheStore(failure=RuntimeError("synthetic cache outage"))
    source, _ = _source(adapter, store=store)

    result = await source.fetch("production_types", _scope())

    assert result.complete and result.cache_source == "live"
    assert len(adapter.calls) == 1


def test_catalog_matches_authoritative_named_collections_and_optional_filters() -> None:
    catalog = load_catalog()
    assert catalog.get("HuohaoQuery").list_key == "huohaoList"
    assert catalog.get("HuohaoQuery").total_key == "hh_total"
    assert catalog.get("HuohaoFormQuery").required_params == ("huohao",)
    assert catalog.get("ScTypeQuery").list_key == "sctypeList"
    assert catalog.get("RfidWorktypeQuery").list_key == "worktypeList"
    assert catalog.get("HuohaoWorktypeQuery").required_params == ()
    assert catalog.get("UserInfoQuery").required_params == ()
    assert catalog.get("MoveMenuQuery").enabled is False


def test_field_knowledge_contains_only_current_v2_fact_semantics() -> None:
    from pathlib import Path

    content = Path("configs/knowledge/field-dictionary.yaml").read_text(encoding="utf-8")
    value = cast(dict[str, object], yaml.safe_load(content))
    metrics = cast(dict[str, dict[str, str]], value["confirmed_metrics"])
    unavailable = cast(dict[str, dict[str, str]], value["unconfirmed_metrics"])
    assert metrics["output.reported_qty"]["formula"] == "sum(GongziClQuery.sl)"
    assert (
        "sum(ScjdQuery.sssl) / sum(ScjdQuery.fhsl)" in metrics["progress.package_ratio"]["formula"]
    )
    assert "progress.finished_packages" in unavailable
    assert "progress.unit_total" in unavailable and "progress.unit_done" in unavailable
    for unsafe in ("default_price", "xz_price", "movepassword", "GongziMxQuery", "YskQuery"):
        assert unsafe not in content
