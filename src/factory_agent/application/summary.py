"""Result answer composition over result metadata and pre-aggregated totals.

The composed answer is one short natural-language reply that combines the
caller's question with the outcome metadata (capability title, time label,
row count, completeness) and the aggregated totals already shown on the
result card. Row-level detail never enters the prompt: the model narrates
from metadata and totals only, and the totals carry no identifiers. A
deterministic fallback keeps the answer available when the model call
fails — a failed answer never fails the interaction.
"""

from dataclasses import dataclass
from decimal import Decimal

from factory_agent.application.structured import StructuredOutputError
from factory_agent.ports import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelStage,
)

#: Persona for the result-answer stage. Zero rows are a normal outcome and
#: must be stated as "no records found", never dressed up as incompleteness
#: and never padded with fabricated numbers.
RESULT_ANSWER_SYSTEM_PROMPT = (
    "你是工厂智能问答助手。根据用户的问题、查询结果元数据和合计统计，"
    "用简洁自然的中文给出回答。\n"
    "回答规则：\n"
    "- 结果为 0 行：明确告知用户在该时间范围内没有查询到相关记录；"
    "空记录是正常结果，不要说“数据不完整”，也不要编造任何数字；\n"
    "- 有数据：优先用“合计统计”里的关键数字组织一句话总结"
    "（如合计金额、总件数、均值等），再引导用户查看下方结果卡片或明细；\n"
    "- 合计统计只取与用户问题最相关的几项，不要全部罗列；"
    "没有合计统计时只说明查询已完成及结果行数；\n"
    "- 结果被标记为不完整（取数失败/分页未取全）时，如实说明本次数据可能不完整；\n"
    "- 绝不复述任何单行明细数值，绝不输出 SQL、URL、员工编号、部门编号、"
    "凭据或任何内部信息；\n"
    "- 回答不超过 120 字。"
)


@dataclass(frozen=True, slots=True)
class SummaryReply:
    """One composed result answer plus the metadata usage events need."""

    text: str
    model_alias: str
    actual_model: str
    duration_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    attempt: int = 1


class ResultSummarizer:
    """Composes the user-facing answer sentence for one capability result."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_alias: str,
        temperature: float = 0.3,
        max_output_tokens: int = 256,
    ) -> None:
        self._gateway = gateway
        self.model_alias = model_alias
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def summarize(
        self,
        *,
        question: str,
        capability_title: str,
        time_label: str,
        row_count: int,
        columns: list[str],
        incomplete: bool,
        incomplete_reason: str | None,
        logical_call_id: str,
        aggregates: list[tuple[str, str]] | None = None,
    ) -> SummaryReply:
        """One SUMMARIZE-stage call; raises gateway/structured errors upward."""
        aggregate_text = "无"
        if aggregates:
            aggregate_text = "；".join(f"{label}：{value}" for label, value in aggregates)
        user_text = (
            f"用户问题：{question}\n"
            f"查询功能：{capability_title}\n"
            f"时间范围：{time_label}\n"
            f"结果行数：{row_count}\n"
            f"结果列：{', '.join(columns) if columns else '无'}\n"
            f"合计统计：{aggregate_text}\n"
            f"结果是否完整：{'是' if not incomplete else f'否（原因：{incomplete_reason}）'}"
        )
        response = await self._gateway.complete(
            ModelRequest(
                model_alias=self.model_alias,
                messages=(
                    ModelMessage(role="system", content=RESULT_ANSWER_SYSTEM_PROMPT),
                    ModelMessage(role="user", content=user_text),
                ),
                stage=ModelStage.SUMMARIZE,
                logical_call_id=logical_call_id,
                json_output=False,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            )
        )
        text = response.content.strip()
        if not text:
            raise StructuredOutputError("model output is empty", attempts=response.attempt)
        return SummaryReply(
            text=text,
            model_alias=self.model_alias,
            actual_model=response.actual_model,
            duration_ms=response.duration_ms,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            cached_tokens=response.usage.cached_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            attempt=response.attempt,
        )


def format_aggregate_value(value: Decimal, column_type: str | None) -> str:
    """Human number for the prompt/answer: money to 2 decimals, others trimmed."""
    if column_type == "money":
        return f"{value.quantize(Decimal('0.01'))}"
    normalized = value.normalize()
    text = format(normalized, "f")
    return text


def fallback_result_answer(
    *,
    row_count: int,
    incomplete: bool,
    incomplete_reason: str | None,
    time_label: str,
    aggregates: list[tuple[str, str]] | None = None,
) -> str:
    """Deterministic answer used when the model call is unavailable.

    Honesty rules mirror the prompt: an empty complete window states that no
    records exist; a degraded fetch never claims "no records" — it says the
    data could not be fully retrieved.
    """
    del incomplete_reason
    if row_count == 0:
        if incomplete:
            return "本次查询未能完整获取数据，暂时无法给出结果，请稍后重试。"
        return f"{time_label}没有查询到相关记录。"
    text = f"查询完成，共 {row_count} 行结果"
    if aggregates:
        text += "，" + "；".join(f"{label} {value}" for label, value in aggregates[:3])
    text += "，详情请查看下方结果卡片。"
    if incomplete:
        text += "注意：本次数据可能不完整。"
    return text


__all__ = [
    "RESULT_ANSWER_SYSTEM_PROMPT",
    "ResultSummarizer",
    "SummaryReply",
    "fallback_result_answer",
    "format_aggregate_value",
]
