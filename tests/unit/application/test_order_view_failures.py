import pytest

from factory_agent.application.session import StartRequest
from factory_agent.domain import INTERACTION_RESULT, Role
from factory_agent.domain.errors import InvalidRequestError
from tests.unit.application.test_order_view_drill import order_drill, reviewed_runner
from tests.unit.application.test_session_service import SESSION, build, credential, drain


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("order_view_snapshot_changed", "数据已刷新，请重新查看汇总后再展开明细。"),
        ("order_view_unavailable", "该明细视图在当前查询范围内不可用，请重新查看汇总。"),
        ("order_view_invalid", "该明细视图在当前查询范围内不可用，请重新查看汇总。"),
    ],
)
@pytest.mark.asyncio
async def test_order_view_rejections_have_friendly_messages_and_specific_categories(
    reason: str, message: str
) -> None:
    runner = reviewed_runner()
    runner.failure = InvalidRequestError(reason)
    service, store, _ = build(contents=["not a model intent"], role=Role.MANAGER, runner=runner)
    record = await service.start(
        credential(), StartRequest(session_id=SESSION, text="展开明细", drill=order_drill())
    )
    events = await drain(service, record.interaction_id)
    assert not any(event.name == INTERACTION_RESULT for event in events)
    assert events[-1].name == "interaction.failed"
    assert events[-1].data["error_category"] == reason
    assert store.interactions[str(record.interaction_id)].error_category == reason
    assert events[-1].data["message"] == message
    assert any(stored.text == message for stored in store.messages)
    assert len(runner.requests) == 1
