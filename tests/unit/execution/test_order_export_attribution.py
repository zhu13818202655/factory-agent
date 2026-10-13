import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from xml.etree import ElementTree

import pytest

from factory_agent.domain import CapabilityId, NarrowedFilters, Role, TenantId, TimeRange
from factory_agent.export.xlsx import render_xlsx
from factory_agent.ports.order_views import OrderViewSelection
from factory_agent.ports.session import CapabilityRunRequest
from tests.support.payload import as_dict, as_list
from tests.unit.execution.test_kernel_multirow import make_multirow_runner
from tests.unit.execution.test_order_views import OrderViewExecutor

_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_PLANS = (None, "未关联计划号", " PLAN ")


def request(capability: str) -> CapabilityRunRequest:
    return CapabilityRunRequest(
        capability_id=CapabilityId(capability),
        role=Role.OWNER,
        filters=NarrowedFilters(
            tenant_id=TenantId("SYNTHETIC-TENANT"), employee_ids=None, dept_ids=None
        ),
        time_range=TimeRange(
            start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)
        ),
    )


def executor() -> OrderViewExecutor:
    return OrderViewExecutor(
        output=tuple(
            {
                "jhdh": plan,
                "huohao": "1250",
                "bbreed": "T5401",
                "worktype": "WT1",
                "wtname": "合成工序",
                "uid": "synthetic-worker",
                "sl": "10",
                "je": "1",
            }
            for plan in _PLANS
        ),
        progress=tuple(
            {
                "jhdh": plan,
                "dh": f"2026090{index}0001",
                "rq": "2026-09-05",
                "huohao": "1250",
                "bbreed": "T5401",
                "bs": "1",
                "fhsl": "10",
                "sssl": "5",
                "wts": "1",
            }
            for index, plan in enumerate(_PLANS, start=1)
        ),
    )


def exported_rows(content: bytes, sheet: int) -> list[list[str]]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        shared = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        values = [
            "".join(text.text or "" for text in entry.findall(".//s:t", _NS))
            for entry in shared.findall("s:si", _NS)
        ]
        worksheet = ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{sheet}.xml"))
        return [
            [
                values[int(cell.findtext("s:v", namespaces=_NS) or "0")]
                if cell.attrib.get("t") == "s"
                else cell.findtext("s:v", default="", namespaces=_NS)
                for cell in row.findall("s:c", _NS)
            ]
            for row in worksheet.findall("s:sheetData/s:row", _NS)
        ]


@pytest.mark.parametrize(
    "capability", ["fr005_order_progress", "fr006_order_output", "fr009_factory_order_overview"]
)
@pytest.mark.asyncio
async def test_export_detail_preserves_group_attribution_without_repeating_it_in_card(
    capability: str,
) -> None:
    source = executor()
    kernel = make_multirow_runner(source)
    result = await kernel.run(request(capability))
    detail = result.detail_tables[0]
    assert {"plan_code", "plan_missing"} <= set(detail.hidden_columns)
    assert detail.columns[0].name == "order_code"
    assert detail.columns[0].title == "计划号"
    assert detail.columns[1].name == "plan_link_state"
    assert detail.columns[1].title == "关联状态"
    assert {(row["order_code"], row["plan_link_state"]) for row in detail.rows} == {
        ("未关联计划号", "未关联"),
        ("未关联计划号", "已关联"),
        (" PLAN ", "已关联"),
    }
    assert result.card is not None
    groups = as_dict(result.card["order_groups"])
    for raw_group in as_list(groups["groups"]):
        group = as_dict(raw_group)
        for raw_view in as_list(group["views"]):
            view = as_dict(raw_view)
            columns = as_list(as_dict(view["table"])["columns"])
            assert "order_code" not in columns
            assert "plan_link_state" not in columns
    calls = tuple(source.calls)
    rows = exported_rows(render_xlsx(kernel.to_render_table(result)), sheet=2)
    assert rows[0][:2] == ["计划号", "关联状态"]
    assert "计划号标识" not in rows[0]
    assert "未关联计划号" not in rows[0]
    assert {tuple(row[:2]) for row in rows[1:4]} == {
        ("未关联计划号", "未关联"),
        ("未关联计划号", "已关联"),
        (" PLAN ", "已关联"),
    }
    assert tuple(source.calls) == calls


@pytest.mark.asyncio
async def test_filtered_detail_export_keeps_all_rows_and_one_selected_plan() -> None:
    source = executor()
    source.progress = tuple(
        {
            "jhdh": "PLAN-A",
            "dh": f"20260901{index:04}",
            "rq": "2026-09-05",
            "bbreed": "T5401",
            "bs": "1",
            "fhsl": "10",
            "sssl": "5" if index % 2 else "10",
            "wts": "1",
        }
        for index in range(1, 26)
    )
    kernel = make_multirow_runner(source)
    selection = OrderViewSelection(
        view_id="sheet_details", group_kind="plan", plan_code="PLAN-A", status="unfinished"
    )
    result = await kernel.run(
        replace(request("fr009_factory_order_overview"), order_view=selection)
    )
    detail = result.detail_tables[0]
    assert len(detail.rows) == 13
    assert all(
        row["order_code"] == "PLAN-A" and row["plan_link_state"] == "已关联" for row in detail.rows
    )
    assert all(row["finish_state"] == "未完工" for row in detail.rows)
    rows = exported_rows(render_xlsx(kernel.to_render_table(result)), sheet=2)
    assert len(rows) == 14
    assert rows[-1][2] == "202609010025"
