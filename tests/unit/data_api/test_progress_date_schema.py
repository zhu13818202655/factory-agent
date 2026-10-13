import pytest
from pydantic import ValidationError

from factory_agent.data_api.schemas import ScjdV2Row


@pytest.mark.parametrize(
    ("returned_date", "expected"),
    [
        ("2026-09-12", "2026-09-12"),
        ("2026-09-12T00:00:00", "2026-09-12"),
        ("2026-09-12 14:35:10", "2026-09-12"),
        ("2026-09-12T00:00:00+08:00", "2026-09-12"),
    ],
)
def test_progress_boundary_keeps_authoritative_date_independent_of_sheet_id(
    returned_date: str, expected: str
) -> None:
    row = ScjdV2Row.model_validate({"dh": "SHEET-SYNTHETIC", "rq": returned_date})
    assert row.dh == "SHEET-SYNTHETIC"
    assert row.rq == expected
    assert row.model_dump()["rq"] == expected


@pytest.mark.parametrize("value", ["2026-02-30", "2026-09-12garbage", "2026-09-12T99:00:00", True])
def test_present_invalid_progress_date_is_rejected_at_the_boundary(value: object) -> None:
    with pytest.raises(ValidationError) as caught:
        ScjdV2Row.model_validate({"dh": "202609120001", "rq": value})
    assert caught.value.errors()[0]["loc"] == ("rq",)


def test_grouped_response_without_date_does_not_infer_it_from_sheet_id() -> None:
    row = ScjdV2Row.model_validate({"dh": "202609120001"})
    assert row.rq == ""
