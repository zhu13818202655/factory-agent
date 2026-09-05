"""Non-business chit-chat reply generation.

A greeting or small-talk turn never touches MES, the permission matrix, or the
capability runner. ``ChatResponder`` is the only session-pipeline caller of the
model gateway for free-form assistant text; the persona forbids fabricating
factory data, and ``compact_history`` keeps business detail rows out of the
prompt (result-bearing turns arrive only as row-count summaries).
"""

from __future__ import annotations

from dataclasses import dataclass

from factory_agent.application.context import ConversationTurn, compact_history
from factory_agent.application.structured import StructuredOutputError
from factory_agent.ports import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelStage,
)

#: Persona used only when the intent parser selects the reserved ``chitchat``
#: capability. It never grants access to business data and tells the model to
#: decline real-time or unverifiable questions instead of guessing.
CHITCHAT_SYSTEM_PROMPT = (
    "你是工厂智能问答助手。除了回答工厂业务查询，你也可以陪用户进行日常闲聊。\n"
    "闲聊回答规则：\n"
    "- 友好、简洁，一般不超过 200 字；\n"
    "- 只谈常识与通用知识，绝不编造任何工厂/MES 的生产、产量或工资数据；\n"
    "- 涉及需要实时数据或无法核实的信息（如今天的天气、实时股价、最新新闻）时，"
    "如实说明“我无法获取实时数据”，不要猜测；\n"
    "- 绝不输出 SQL、URL、员工编号、部门编号、凭据或任何内部信息。"
)


@dataclass(frozen=True, slots=True)
class ChatReply:
    """One generated free-form reply plus the metadata usage events need."""

    text: str
    model_alias: str
    actual_model: str
    duration_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    attempt: int = 1


class ChatResponder:
    """Turns a non-business utterance into a short free-form reply."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_alias: str,
        max_history_turns: int = 8,
        max_history_chars: int = 8192,
        temperature: float = 0.7,
        max_output_tokens: int = 512,
    ) -> None:
        self._gateway = gateway
        self.model_alias = model_alias
        self._max_history_turns = max_history_turns
        self._max_history_chars = max_history_chars
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def reply(
        self,
        user_text: str,
        *,
        logical_call_id: str,
        history: tuple[ConversationTurn, ...] = (),
    ) -> ChatReply:
        """Generate one reply; raises gateway/``StructuredOutputError`` upward."""
        if not user_text.strip():
            raise StructuredOutputError("user text is empty", attempts=0)
        response = await self._gateway.complete(
            ModelRequest(
                model_alias=self.model_alias,
                messages=self._messages(user_text, history),
                stage=ModelStage.CHAT,
                logical_call_id=logical_call_id,
                json_output=False,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            )
        )
        text = response.content.strip()
        if not text:
            raise StructuredOutputError("model output is empty", attempts=response.attempt)
        return ChatReply(
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

    def _messages(
        self, user_text: str, history: tuple[ConversationTurn, ...]
    ) -> tuple[ModelMessage, ...]:
        compacted = compact_history(
            history, max_turns=self._max_history_turns, max_chars=self._max_history_chars
        )
        return (
            ModelMessage(role="system", content=CHITCHAT_SYSTEM_PROMPT),
            *compacted,
            ModelMessage(role="user", content=user_text),
        )


__all__ = [
    "CHITCHAT_SYSTEM_PROMPT",
    "ChatReply",
    "ChatResponder",
]
