"""Unit tests for the non-business chit-chat responder.

The responder is the only session-pipeline path that generates free-form
assistant text: requests must use ``ModelStage.CHAT`` with JSON output off and
a persona that never fabricates factory data.
"""

from __future__ import annotations

import pytest

from factory_agent.application.chitchat import ChatResponder
from factory_agent.application.context import ConversationTurn
from factory_agent.application.structured import StructuredOutputError
from factory_agent.domain import CapabilityId, InteractionStatus
from factory_agent.ports import ModelStage
from tests.support.session import ScriptedModelGateway

REPLY = "你好呀！我是工厂助手，有什么可以帮你的吗？"


@pytest.mark.asyncio
async def test_reply_returns_free_form_text_with_a_chat_stage_request() -> None:
    gateway = ScriptedModelGateway(contents=[REPLY])

    result = await ChatResponder(gateway, model_alias="factory-summary").reply(
        "你好", logical_call_id="c1"
    )

    assert result.text == REPLY
    assert result.actual_model == gateway.actual_model
    assert result.model_alias == "factory-summary"
    request = gateway.requests[0]
    assert request.stage is ModelStage.CHAT
    assert request.json_output is False
    assert request.messages[0].role == "system"
    assert "闲聊" in request.messages[0].content
    assert request.messages[-1].content == "你好"


@pytest.mark.asyncio
async def test_empty_user_text_never_reaches_the_gateway() -> None:
    gateway = ScriptedModelGateway(contents=[REPLY])

    with pytest.raises(StructuredOutputError):
        await ChatResponder(gateway, model_alias="factory-summary").reply(
            "   ", logical_call_id="c1"
        )

    assert gateway.requests == []


@pytest.mark.asyncio
async def test_empty_model_output_is_rejected() -> None:
    responder = ChatResponder(
        ScriptedModelGateway(contents=["   \n"]), model_alias="factory-summary"
    )

    with pytest.raises(StructuredOutputError):
        await responder.reply("你好", logical_call_id="c1")


@pytest.mark.asyncio
async def test_compacted_history_is_included_after_the_persona() -> None:
    gateway = ScriptedModelGateway(contents=[REPLY])
    history = (
        ConversationTurn(
            user_text="上个月产量",
            assistant_text="返回了 FR-001 的结果表（3 行）。",
            status=InteractionStatus.COMPLETED,
            capability_id=CapabilityId("FR-001"),
            result_row_count=3,
        ),
    )

    await ChatResponder(gateway, model_alias="factory-summary").reply(
        "那太棒了", logical_call_id="c1", history=history
    )

    request = gateway.requests[0]
    assert [message.role for message in request.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert request.messages[1].content == "上个月产量"
