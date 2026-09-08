import pytest

from factory_agent.application.summary import (
    RESULT_ANSWER_SYSTEM_PROMPT,
    ResultSummarizer,
    fallback_result_answer,
)
from factory_agent.ports.model import (
    ModelErrorCategory,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelStage,
    ModelUsage,
)


class FakeGateway:
    def __init__(self, content: str = "本月没有查询到您的计件记录。") -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            content=self.content,
            actual_model="fake-model",
            usage=ModelUsage(prompt_tokens=10, completion_tokens=5),
            duration_ms=12,
        )


class FailingGateway:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelGatewayError(ModelErrorCategory.TIMEOUT, "upstream timeout", duration_ms=8)


@pytest.mark.asyncio
async def test_summarize_sends_metadata_only_prompt() -> None:
    gateway = FakeGateway()
    summarizer = ResultSummarizer(gateway, model_alias="factory-summary")
    reply = await summarizer.summarize(
        question="我这个月的个人产量是多少?",
        capability_title="个人产量统计",
        time_label="本月",
        row_count=0,
        columns=["rq", "huohao", "worktype", "output_qty"],
        incomplete=False,
        incomplete_reason=None,
        logical_call_id="call-1",
    )
    assert reply.text == "本月没有查询到您的计件记录。"
    assert reply.actual_model == "fake-model"
    request = gateway.requests[0]
    assert request.stage is ModelStage.SUMMARIZE
    assert request.messages[0].content == RESULT_ANSWER_SYSTEM_PROMPT
    user_text = request.messages[1].content
    assert "个人产量统计" in user_text
    assert "结果行数：0" in user_text
    # Row values never enter the prompt; only metadata does.
    assert "2835" not in user_text


@pytest.mark.asyncio
async def test_fallback_empty_complete_window_states_no_records() -> None:
    text = fallback_result_answer(
        row_count=0, incomplete=False, incomplete_reason=None, time_label="本月"
    )
    assert text == "本月没有查询到相关记录。"


@pytest.mark.asyncio
async def test_fallback_empty_incomplete_never_claims_no_records() -> None:
    text = fallback_result_answer(
        row_count=0, incomplete=True, incomplete_reason="upstream_timeout", time_label="本月"
    )
    assert "没有查询到" not in text
    assert "稍后重试" in text


@pytest.mark.asyncio
async def test_fallback_rows_present_mentions_incompleteness() -> None:
    text = fallback_result_answer(
        row_count=12, incomplete=False, incomplete_reason=None, time_label="本月"
    )
    assert "12 行" in text
    warned = fallback_result_answer(
        row_count=12, incomplete=True, incomplete_reason="pagination_total_drift", time_label="本月"
    )
    assert "不完整" in warned


@pytest.mark.asyncio
async def test_aggregates_reach_the_summarizer_prompt() -> None:
    """Totals shown on the card may be narrated: they ride into the prompt."""
    gateway = FakeGateway()
    summarizer = ResultSummarizer(gateway, model_alias="factory-summary")
    await summarizer.summarize(
        question="我上个月的工资是多少?",
        capability_title="个人工资汇总",
        time_label="上个月",
        row_count=107,
        columns=["gross_total", "piece_count", "daily_avg"],
        incomplete=False,
        incomplete_reason=None,
        logical_call_id="call-agg",
        aggregates=[("计件工资合计（元）", "439.95"), ("计件件数（件）", "2933")],
    )
    user_text = gateway.requests[0].messages[1].content
    assert "439.95" in user_text
    assert "计件件数（件）：2933" in user_text


def test_format_aggregate_value_money_two_decimals() -> None:
    from decimal import Decimal

    from factory_agent.application.summary import format_aggregate_value

    assert format_aggregate_value(Decimal("14.19193548"), "money") == "14.19"
    assert format_aggregate_value(Decimal("2933"), "quantity") == "2933"
    assert format_aggregate_value(Decimal("2933.500"), None) == "2933.5"
