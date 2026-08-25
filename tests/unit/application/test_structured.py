from __future__ import annotations

import pytest

from factory_agent.application.structured import (
    REPAIR_INSTRUCTION,
    StructuredOutputError,
    extract_json_object,
    request_structured_object,
)
from factory_agent.ports import (
    ModelErrorCategory,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelStage,
)
from tests.support.session import ScriptedModelGateway


def request() -> ModelRequest:
    return ModelRequest(
        model_alias="factory-fast",
        messages=(ModelMessage(role="user", content="本月产量"),),
        stage=ModelStage.EXTRACT,
        logical_call_id="call-1",
        json_output=True,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"capability_id": "FR-001"}', {"capability_id": "FR-001"}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('这是结果：{"a": 1}，请查收', {"a": 1}),
        ("", None),
        ("   ", None),
        ("[1, 2, 3]", None),
        ("not json at all", None),
    ],
)
def test_json_extraction_shapes(text: str, expected: dict[str, object] | None) -> None:
    assert extract_json_object(text) == expected


@pytest.mark.asyncio
async def test_valid_payload_needs_no_repair() -> None:
    gateway = ScriptedModelGateway(contents=['{"capability_id": "FR-001"}'])

    result = await request_structured_object(gateway, request())

    assert result.attempts == 1
    assert result.repaired is False
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_one_semantic_repair_is_attempted_at_most_once() -> None:
    gateway = ScriptedModelGateway(contents=["抱歉，我不确定", '{"capability_id": "FR-001"}'])

    result = await request_structured_object(gateway, request(), max_repair_attempts=1)

    assert result.repaired is True
    assert result.attempts == 2
    repair_request = gateway.requests[1]
    assert repair_request.stage is ModelStage.REPAIR
    assert repair_request.messages[-1].content == REPAIR_INSTRUCTION


@pytest.mark.asyncio
async def test_a_second_unusable_reply_is_a_semantic_failure() -> None:
    gateway = ScriptedModelGateway(contents=["nope", "still nope"])

    with pytest.raises(StructuredOutputError) as failure:
        await request_structured_object(gateway, request(), max_repair_attempts=1)

    assert failure.value.attempts == 2
    assert len(gateway.requests) == 2


@pytest.mark.asyncio
async def test_repair_can_be_disabled() -> None:
    gateway = ScriptedModelGateway(contents=["nope"])

    with pytest.raises(StructuredOutputError):
        await request_structured_object(gateway, request(), max_repair_attempts=0)

    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_gateway_failures_are_not_converted_into_semantic_failures() -> None:
    gateway = ScriptedModelGateway(
        failures=[ModelGatewayError(ModelErrorCategory.TIMEOUT, "gateway request timed out")]
    )

    with pytest.raises(ModelGatewayError) as failure:
        await request_structured_object(gateway, request())

    assert failure.value.category is ModelErrorCategory.TIMEOUT
