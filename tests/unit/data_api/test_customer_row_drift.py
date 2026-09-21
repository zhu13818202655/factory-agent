"""Contract-drift leniency at the customer row boundary.

Real-environment decision (2026-09-13): a field the model author marked as
not consumed (it declares a default) must not fail the interaction when the
customer MES stops sending it — the default is filled and one warning is
logged per (model, field). Fields without a default stay required, so drift
that removes a consumed value still fails closed (see
``tests/unit/execution/test_kernel_integration.py::test_contract_drift_is_structured_upstream_invalid``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

# 直接引用模块私有名是刻意的：本文件测的就是 ``_CustomerRow`` 的宽松校验语义与
# ``_WARNED_MISSING_FIELDS`` 的去重状态，而该模块没有可供断言的公开接缝。
from factory_agent.data_api.schemas import (
    _WARNED_MISSING_FIELDS,  # pyright: ignore[reportPrivateUsage]
    _CustomerRow,  # pyright: ignore[reportPrivateUsage]
)


class _DriftRow(_CustomerRow):
    uid: str
    remark: str = ""
    tags: list[str] = []


@pytest.fixture(autouse=True)
def _reset_warned_fields() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """每个用例前清空告警去重状态（fixture 由 pytest 收集，故 pyright 视为未使用）。"""
    _WARNED_MISSING_FIELDS.clear()
    yield
    _WARNED_MISSING_FIELDS.clear()


def test_absent_defaulted_field_is_filled_and_warned_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        first = _DriftRow.model_validate({"uid": "U1"})
        second = _DriftRow.model_validate({"uid": "U2"})

    # Defaults filled, row still usable.
    assert first.uid == "U1"
    assert first.remark == ""
    assert first.tags == []
    assert second.uid == "U2"

    # One warning per (model, field) per process — not one per row.
    warnings = [r for r in caplog.records if "mes.row.field_missing" in r.getMessage()]
    assert len(warnings) == 2  # remark + tags, exactly once each

    # A repeat row emits nothing further.
    _DriftRow.model_validate({"uid": "U3"})
    warnings_after = [r for r in caplog.records if "mes.row.field_missing" in r.getMessage()]
    assert len(warnings_after) == 2


def test_explicit_values_are_kept_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        row = _DriftRow.model_validate({"uid": "U1", "remark": "kept", "tags": ["a"]})

    assert row.remark == "kept"
    assert row.tags == ["a"]
    assert "mes.row.field_missing" not in caplog.text


def test_absent_required_field_still_fails_closed() -> None:
    with pytest.raises(ValidationError):
        _DriftRow.model_validate({"remark": "no uid"})


def test_absent_required_field_fails_but_null_required_field_is_coerced() -> None:
    # A required field MISSING from the payload stays fail-closed: no default
    # is fabricated for a consumed value.
    with pytest.raises(ValidationError):
        _DriftRow.model_validate({"remark": "no uid"})

    # A null value for a required str field is the pre-existing ``_coerce_scalars``
    # null -> "" normalisation (nullable upstream timestamps), not leniency drift.
    row = _DriftRow.model_validate({"uid": None})
    assert row.uid == ""
