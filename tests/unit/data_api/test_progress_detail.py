from collections.abc import Mapping
from typing import cast

import pytest

from factory_agent.data_api.progress_detail import (
    ProgressDerivationError,
    aggregate_progress_rows,
    derive_progress_rows,
)

M7 = ("dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname")
PACKAGE = ("dh", "baohao", "userid", "color", "chima")
WORKTYPE = ("userid", "uid", "uname", "dept", "deptname", "worktype", "wtname", "wsort")


def detail(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "dh": "202607010001",
        "jhdh": "PLAN-A",
        "chuanghao": "BED-A",
        "huohao": "STYLE-A",
        "bbreed": "Style A",
        "baohao": "1",
        "userid": "MATERIAL-A",
        "color": "Color A",
        "chima": "Size A",
        "uid": "EMP-A",
        "uname": "Employee A",
        "dept": "DEPT-A",
        "deptname": "Department A",
        "worktype": "WORK-A",
        "wtname": "Work A",
        "wsort": "1",
        "zhgx": "0",
        "bs": "2",
        "fhsl": "10",
        "sssl": "5",
        "zpsl": "4",
        "wgs": "1",
        "wts": "2",
        "wcl": "50.00",
    }
    row.update(changes)
    return row


def test_default_detail_derives_calendar_date_without_mutating_input() -> None:
    row = detail(dh="202402290001", dept=None, deptname=None, uid=None, uname=None, wgs="0")

    rows = aggregate_progress_rows((row,), ())

    assert rows[0]["dh_date"] == "2024-02-29"
    assert rows[0]["dept"] == rows[0]["uid"] == ""
    assert rows[0]["wgs"] == "0"
    assert "dh_date" not in row


@pytest.mark.parametrize("sheet_id", [None, "", "20260230001", "2026-07-01", "２０２６０７０１"])
def test_invalid_sheet_date_rejects_whole_set_without_exposing_values(sheet_id: object) -> None:
    with pytest.raises(ProgressDerivationError, match="invalid_sheet_date") as caught:
        derive_progress_rows((detail(), detail(dh=sheet_id)))

    assert str(caught.value) == "progress detail derivation failed: invalid_sheet_date"


def test_m7_sums_customer_contributions_across_material_worktype_and_employee_rows() -> None:
    rows = (
        detail(),
        detail(uid="EMP-B", uname="Employee B", sssl="2", zpsl="2", wgs="3", bs="4"),
        detail(worktype="WORK-B", wtname="Work B", wsort="2", fhsl="20", sssl="18"),
        detail(userid="MATERIAL-B", baohao="2", fhsl="60", sssl="55", wcl="91.67"),
    )

    result = aggregate_progress_rows(rows, M7)

    assert len(result) == 1
    assert result[0] == {
        "dh": "202607010001",
        "jhdh": "PLAN-A",
        "chuanghao": "BED-A",
        "huohao": "STYLE-A",
        "bbreed": "Style A",
        "dept": "DEPT-A",
        "deptname": "Department A",
        "bs": "10",
        "fhsl": "100",
        "sssl": "80",
        "zpsl": "14",
        "wgs": "6",
        "wts": "8",
        "wcl": "80.00",
    }
    assert result == aggregate_progress_rows(tuple(reversed(rows)), M7)


def test_package_grain_keeps_materials_separate_and_recomputes_weighted_ratio() -> None:
    rows = (
        detail(),
        detail(worktype="WORK-B", wtname="Work B", wsort="2", fhsl="90", sssl="0", wcl="0"),
        detail(userid="MATERIAL-B", baohao="2", fhsl="3", sssl="1", wcl="33.3333"),
    )

    result = aggregate_progress_rows(rows, PACKAGE)

    by_material = {row["userid"]: row for row in result}
    assert by_material["MATERIAL-A"]["fhsl"] == "100"
    assert by_material["MATERIAL-A"]["sssl"] == "5"
    assert by_material["MATERIAL-A"]["wcl"] == "5.00"
    assert by_material["MATERIAL-A"]["wts"] == "4"
    assert by_material["MATERIAL-B"]["wcl"] == "33.33"
    assert all("dh_date" not in row for row in result)


def test_worktype_grain_retains_scanned_and_unscanned_workers() -> None:
    rows = (
        detail(),
        detail(uid="EMP-B", uname="Employee B", sssl="3"),
        detail(
            worktype="WORK-B",
            wtname="Work B",
            wsort="2",
            uid=None,
            uname=None,
            dept=None,
            deptname=None,
            wgs="0",
            sssl="0",
            zpsl="0",
        ),
    )

    result = aggregate_progress_rows(rows, WORKTYPE)

    assert len(result) == 3
    assert {row["uid"] for row in result} == {"EMP-A", "EMP-B", ""}
    unscanned = next(row for row in result if row["uid"] == "")
    assert unscanned["dept"] == ""
    assert unscanned["wgs"] == "0"
    assert unscanned["wts"] == "2"
    assert unscanned["wcl"] == "0.00"


def test_null_department_contributions_are_not_merged_into_named_departments() -> None:
    rows = (detail(), detail(dept=None, deptname=None, uid=None, uname=None, wgs="0"))

    result = aggregate_progress_rows(rows, M7)

    assert len(result) == 2
    assert {row["dept"] for row in result} == {"", "DEPT-A"}
    assert sum(int(row["fhsl"]) for row in result) == 20


def test_zero_denominator_returns_zero_without_reusing_row_ratio() -> None:
    result = aggregate_progress_rows((detail(fhsl="0", sssl="9", wcl="999"),), PACKAGE)

    assert result[0]["wcl"] == "0.00"


def test_dimension_codes_and_names_share_the_same_group_and_reject_ambiguous_names() -> None:
    result = aggregate_progress_rows((detail(), detail(sssl="8")), M7 + ("dept",))

    assert len(result) == 1
    assert result[0]["deptname"] == "Department A"
    assert result[0]["sssl"] == "13"
    with pytest.raises(ProgressDerivationError, match="inconsistent_dimension_name"):
        aggregate_progress_rows((detail(), detail(deptname="Department B")), M7)


@pytest.mark.parametrize("value", [None, "", "not-a-number", "NaN", "Infinity", "-1"])
def test_missing_or_invalid_contributions_are_not_fabricated_as_zero(value: object) -> None:
    with pytest.raises(ProgressDerivationError):
        aggregate_progress_rows((detail(fhsl=value),), M7)


def test_missing_dimension_is_not_fabricated_as_empty() -> None:
    row = detail()
    row.pop("dept")

    with pytest.raises(ProgressDerivationError, match="missing_dimension"):
        aggregate_progress_rows((row,), M7)


def test_unsupported_grain_does_not_guess_pure_sheet_package_statistics() -> None:
    with pytest.raises(ProgressDerivationError, match="unsupported_grain"):
        aggregate_progress_rows((detail(),), ("dh", "jhdh", "chuanghao", "huohao", "bbreed"))

    assert all(
        "bgs" not in row and "bts" not in row for row in aggregate_progress_rows((detail(),), M7)
    )


def test_empty_sets_and_group_field_permutations_are_deterministic() -> None:
    assert aggregate_progress_rows((), M7) == ()
    assert aggregate_progress_rows((), ()) == ()
    rows = (detail(dept="DEPT-B", deptname="Department B"), detail())

    assert aggregate_progress_rows(rows, M7) == aggregate_progress_rows(rows, tuple(reversed(M7)))


def test_nested_customer_values_are_rejected_without_exposing_payload() -> None:
    row = detail(color={"untrusted": "sensitive-canary"})

    with pytest.raises(ProgressDerivationError, match="invalid_scalar") as caught:
        derive_progress_rows((cast(Mapping[str, object], row),))

    assert "sensitive-canary" not in str(caught.value)
