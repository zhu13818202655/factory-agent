"""GongziMxQuery response-shape tests.

The real MES (联调 2026-09-06, 差异台账待客户确认) returns two distinct row
shapes for the single operation, selected by ``scheme``:
- scheme "" (detail, contract §8.1) -> ``GongziMxRow``;
- scheme hz/HZ/汇总 (summary) -> ``GongziMxSummaryRow`` (shape not documented
  in §8.1: no id/rq/..., adds bs and *_raw/*_src fields).

The adapter selects the row model by scheme so neither shape is rejected as an
unmodeled external shape, and each model still fails closed on the other
shape's rows.
"""


from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from factory_agent.application.filters import NarrowedFilters
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.data_api.schemas import (
    GongziMxRow,
    GongziMxSummaryRow,
    gongzi_mx_row_model_for,
    is_gongzi_mx_summary_scheme,
)
from factory_agent.domain import EmployeeId, TenantId, UserId

#: One row captured from the REAL MES summary response (scheme=hz), employee
#: 1642, 2026-09-06 joint debugging.
_REAL_SUMMARY_ROW: dict[str, Any] = {
    "chuanghao": "1022",
    "huohao": "2976后片",
    "uid": "1642",
    "uname": "汪云梅",
    "dept": "001",
    "type": "扫码产量",
    "worktype": "后片压线",
    "bs": 10,
    "fhsl": 252.0,
    "sl": 252.0,
    "price": 0.15,
    "je": 37.8,
    "huohao_raw": None,
    "huohao_src": None,
    "worktype_raw": None,
    "worktype_src": None,
}

#: One row captured from the REAL MES detail response (scheme="", §8.1).
_REAL_DETAIL_ROW: dict[str, Any] = {
    "id": 439079,
    "type": "扫码产量",
    "rq": "2026-07-31T17:48:33.303",
    "inputtime": "07-31 17:48:33",
    "uid": "1642",
    "uname": "汪云梅",
    "dept": "001",
    "chuanghao": "887",
    "baohao": "39",
    "huohao": "2976后片",
    "color": "卡其棕",
    "chima": "M",
    "worktype": "后片压线",
    "ischeck": 0,
    "check_time": "",
    "fhsl": 42.0,
    "sl": 42.0,
    "price": 0.15,
    "je": 6.3,
    "inputtime_raw": None,
    "check_time_raw": None,
}


def test_summary_scheme_detection() -> None:
    assert is_gongzi_mx_summary_scheme("hz")
    assert is_gongzi_mx_summary_scheme("HZ")
    assert is_gongzi_mx_summary_scheme("汇总")
    assert is_gongzi_mx_summary_scheme("  hz  ")
    assert not is_gongzi_mx_summary_scheme("")
    assert not is_gongzi_mx_summary_scheme(None)
    assert not is_gongzi_mx_summary_scheme("货号工序")


def test_row_model_is_selected_by_scheme() -> None:
    assert gongzi_mx_row_model_for("hz") is GongziMxSummaryRow
    assert gongzi_mx_row_model_for("汇总") is GongziMxSummaryRow
    assert gongzi_mx_row_model_for("") is GongziMxRow
    assert gongzi_mx_row_model_for(None) is GongziMxRow


def test_real_summary_row_validates_only_against_summary_model() -> None:
    GongziMxSummaryRow.model_validate(dict(_REAL_SUMMARY_ROW))
    with pytest.raises(ValidationError):
        GongziMxRow.model_validate(dict(_REAL_SUMMARY_ROW))


def test_real_detail_row_validates_only_against_detail_model() -> None:
    GongziMxRow.model_validate(dict(_REAL_DETAIL_ROW))
    with pytest.raises(ValidationError):
        GongziMxSummaryRow.model_validate(dict(_REAL_DETAIL_ROW))


def _bundle() -> MesCredentialBundle:
    return MesCredentialBundle(
        access_token="mock-access-token",
        app_key="APPKEY-A",
        sign="mock-sign",
        timestamp=1,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("1642"),
        uname="汪云梅",
    )


def _filters() -> NarrowedFilters:
    return NarrowedFilters(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId("1642")}),
        dept_ids=frozenset(),
    )


def _range() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 31, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_fetch_resource_validates_summary_shape_for_scheme_hz() -> None:
    """A scheme=hz page (real summary shape) must fetch and validate cleanly."""
    from tests.support.http_stubs import JsonBodyTransport

    envelope = {
        "code": 1,
        "message": "成功",
        "result": {
            "list": [dict(_REAL_SUMMARY_ROW)],
            "total": 1,
            "footer": {"bs_total": "1", "je_total": "37.8"},
        },
        "timestamp": 1,
    }
    adapter = HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        load_catalog(),
        client=JsonBodyTransport(envelope).client(),
    )
    fetched = await adapter.fetch_resource(
        "GongziMxQuery",
        _filters(),
        _range(),
        page_size=200,
        extra_params={"scheme": "hz", "Type": "0,1,2", "Flag": "0", "queryFooter": "1"},
    )
    assert fetched.complete is True
    assert len(fetched.rows) == 1
    row = fetched.rows[0]
    assert row["je"] == "37.8"
    assert row["bs"] == "10"
    assert row["huohao_raw"] == ""
    await adapter.aclose()


@pytest.mark.asyncio
async def test_fetch_resource_validates_detail_shape_for_empty_scheme() -> None:
    """A scheme="" page (detail shape, §8.1) still validates against the
    detail model after the scheme-aware selection is added."""
    from tests.support.http_stubs import JsonBodyTransport

    envelope = {
        "code": 1,
        "message": "成功",
        "result": {
            "list": [dict(_REAL_DETAIL_ROW)],
            "total": 1,
            "footer": {"sl_total": "42", "je_total": "6.3"},
        },
        "timestamp": 1,
    }
    adapter = HongzhaoMesAdapter(
        "http://mock.invalid",
        _bundle(),
        load_catalog(),
        client=JsonBodyTransport(envelope).client(),
    )
    fetched = await adapter.fetch_resource(
        "GongziMxQuery",
        _filters(),
        _range(),
        page_size=200,
        extra_params={"scheme": "", "Type": "0,1,2", "Flag": "0", "queryFooter": "1"},
    )
    assert fetched.complete is True
    assert len(fetched.rows) == 1
    assert fetched.rows[0]["rq"] == "2026-07-31T17:48:33.303"
    assert fetched.rows[0]["id"] == "439079"
    await adapter.aclose()
