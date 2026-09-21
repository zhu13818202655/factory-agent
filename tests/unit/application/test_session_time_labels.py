"""时间窗展示标签的本地日期回归测试。

Canonical 时间窗以 UTC 存储；导出文件名（``time_range_label``）与卡片日期
（``_result_time_label`` 回退分支）必须展示**厂区本地含端日期**，不能直取
UTC ``.date()``（曾把「上个月」标签显示成 2026-07-31_2026-08-31，2026-09-16 实测）。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

from factory_agent.application.session.pipeline import (
    RunState,
    SessionPipelineMixin,
    time_range_label,
)
from factory_agent.domain import TimeRange

_ZONE = ZoneInfo("Asia/Shanghai")


def _stub_state(time_expression: str | None) -> RunState:
    """Minimal RunState stand-in: only ``last_intent`` is read.

    用 ``cast`` 而不是构造完整 ``RunState``：被测标签只读
    ``last_intent.slots.time_expression``，为此准备 record / sequence /
    started_monotonic / transcript 是在测夹具而非测标签。
    """
    if time_expression is None:
        return cast("RunState", SimpleNamespace(last_intent=None))
    return cast(
        "RunState",
        SimpleNamespace(
            last_intent=SimpleNamespace(slots=SimpleNamespace(time_expression=time_expression))
        ),
    )


def test_export_label_maps_utc_window_to_local_dates() -> None:
    """线上回归：UTC 窗直取 .date() 曾给出 2026-07-31_2026-08-31。"""
    window = TimeRange(
        start=datetime(2026, 7, 31, 16, 0, tzinfo=UTC),  # 2026-08-01T00:00+08:00
        end=datetime(2026, 8, 31, 16, 0, tzinfo=UTC),  # 2026-09-01T00:00+08:00
    )
    assert time_range_label(window, _ZONE) == "2026-08-01_2026-08-31"


def test_export_label_end_is_the_inclusive_last_local_day() -> None:
    """半开区间 [08-01, 09-01) 的标签末端必须是含端的 08-31，不是 09-01。"""
    window = TimeRange(
        start=datetime(2026, 8, 1, tzinfo=_ZONE),
        end=datetime(2026, 9, 1, tzinfo=_ZONE),
    )
    assert time_range_label(window, _ZONE) == "2026-08-01_2026-08-31"


def test_result_label_prefers_the_caller_time_expression() -> None:
    window = TimeRange(
        start=datetime(2026, 8, 1, tzinfo=_ZONE),
        end=datetime(2026, 9, 1, tzinfo=_ZONE),
    )
    state = _stub_state("上个月")
    # 私有静态助手无公开接缝，但它是本回归的修复点本身（UTC 窗直取 .date() 的
    # 回退分支），必须直测；因此就地抑制这一条诊断，而不是放弃覆盖。
    label = SessionPipelineMixin._result_time_label(  # pyright: ignore[reportPrivateUsage]
        state, window, _ZONE
    )
    assert label == "上个月"


def test_result_label_fallback_uses_inclusive_local_dates() -> None:
    window = TimeRange(
        start=datetime(2026, 7, 31, 16, 0, tzinfo=UTC),
        end=datetime(2026, 8, 31, 16, 0, tzinfo=UTC),
    )
    state = _stub_state(None)
    label = SessionPipelineMixin._result_time_label(  # pyright: ignore[reportPrivateUsage]
        state, window, _ZONE
    )
    assert label == "2026-08-01 至 2026-08-31"
