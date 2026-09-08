"""Pre-execution scope guard: the dedicated permission chain (方案二).

Runs after intent selection and the capability-role matrix, before any
business-data call or filter narrowing. The model only *classifies* the data
scope the user is asking for; the decision to deny narrows access and never
grants it — the authoritative bounds stay the token role matrix and MES-side
row filtering (``DataScope.mes_filtered``). On any model failure the guard
fails open: the run proceeds and MES filtering still bounds every row, so
availability never depends on the model while data safety never depends on it
either.
"""


from dataclasses import dataclass

from factory_agent.application.permission_matrix import ROLE_DATA_RANGE
from factory_agent.application.structured import (
    StructuredOutputError,
    request_structured_object,
)
from factory_agent.domain import Role
from factory_agent.ports import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelStage,
)

#: Persona for the SCOPE_GUARD stage. It sees only the caller's role, the
#: authoritative range text, and the user's question — never any business row.
SCOPE_GUARD_SYSTEM_PROMPT = (
    "你是工厂问答助手的权限范围审核器。根据调用者的角色与可查询范围，"
    "判断用户问题所请求的数据范围是否超出其权限。\n"
    "判断规则：\n"
    "- 只判断“请求的数据范围”，不判断问题本身是否合理、不改写问题；\n"
    "- “我的/本人的/我自己的”永远在范围内；\n"
    "- 提到超出调用者范围的对象（如普通员工问全组/全车间/全厂/某个同事的数据，"
    "组长问其他组/其他部门，管理问非本人管辖部门，或任何人问全厂）判为 beyond；\n"
    "- 问的是自己管辖范围内的（如组长问“我们组”、管理问“我们车间”）判为 within；\n"
    "- 无法确定时判为 within（后续系统仍会由 MES 按权限过滤数据）；\n"
    "- 绝不输出任何业务数据、SQL、URL、员工编号、部门编号或内部信息。\n"
    '只输出一个 JSON 对象：{"verdict": "within" 或 "beyond", '
    '"target": "超出范围时用不超过 20 字描述请求对象（如：全组的工资明细），within 时为空字符串"}'
)

#: MES token role codes (00 员工 / 01 组长 / 02 管理 / 99 老板) and labels for
#: the guard prompt — the enum member names are never shown to the model.
_ROLE_PROMPT_NAMES: dict[Role, str] = {
    Role.EMPLOYEE: "00（普通员工）",
    Role.GROUP_LEADER: "01（组长）",
    Role.MANAGER: "02（管理）",
    Role.OWNER: "99（老板）",
}

_TARGET_MAX_CHARS = 40


@dataclass(frozen=True, slots=True)
class ScopeVerdict:
    """One classified request scope plus the metadata usage events need."""

    beyond: bool
    target: str
    model_alias: str
    actual_model: str
    duration_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    attempt: int = 1
    repaired: bool = False


def deny_message(role: Role, target: str | None) -> str:
    """Deterministic user-facing denial: the model never writes the final text."""
    range_text = ROLE_DATA_RANGE[role]
    requested = _clean_target(target) or "该范围的数据"
    return f"抱歉，您没有权限查询{requested}。您当前可查询的范围是：{range_text}。"


def _clean_target(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    if len(text) > _TARGET_MAX_CHARS:
        text = text[:_TARGET_MAX_CHARS]
    return text


class ScopeGuard:
    """Classifies whether one question asks for data beyond the caller's range."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        model_alias: str,
        temperature: float = 0.0,
        max_output_tokens: int = 256,
    ) -> None:
        self._gateway = gateway
        self.model_alias = model_alias
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def check(
        self,
        *,
        question: str,
        capability_title: str,
        role: Role,
        logical_call_id: str,
    ) -> ScopeVerdict:
        """One SCOPE_GUARD call; raises gateway/``StructuredOutputError`` upward."""
        range_text = ROLE_DATA_RANGE[role]
        user_text = (
            f"调用者角色：{_ROLE_PROMPT_NAMES[role]}\n"
            f"可查询范围：{range_text}\n"
            f"本次命中的查询功能：{capability_title}\n"
            f"用户问题：{question}"
        )
        result = await request_structured_object(
            self._gateway,
            ModelRequest(
                model_alias=self.model_alias,
                messages=(
                    ModelMessage(role="system", content=SCOPE_GUARD_SYSTEM_PROMPT),
                    ModelMessage(role="user", content=user_text),
                ),
                stage=ModelStage.SCOPE_GUARD,
                logical_call_id=logical_call_id,
                json_output=True,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            ),
        )
        verdict = result.payload.get("verdict")
        if verdict not in ("within", "beyond"):
            raise StructuredOutputError(
                "scope verdict is missing or invalid", attempts=result.attempts
            )
        return ScopeVerdict(
            beyond=verdict == "beyond",
            target=_clean_target(result.payload.get("target")),
            model_alias=self.model_alias,
            actual_model=result.response.actual_model,
            duration_ms=result.response.duration_ms,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=result.response.usage.completion_tokens,
            cached_tokens=result.response.usage.cached_tokens,
            reasoning_tokens=result.response.usage.reasoning_tokens,
            attempt=result.attempts,
            repaired=result.repaired,
        )


__all__ = [
    "SCOPE_GUARD_SYSTEM_PROMPT",
    "ScopeGuard",
    "ScopeVerdict",
    "deny_message",
]
