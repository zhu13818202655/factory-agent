import pytest

from factory_agent.application.scope_guard import (
    SCOPE_GUARD_SYSTEM_PROMPT,
    ScopeGuard,
    deny_message,
)
from factory_agent.domain import Role
from factory_agent.ports import ModelErrorCategory, ModelGatewayError
from tests.support.session import ScriptedModelGateway

WITHIN = '{"verdict": "within", "target": ""}'
BEYOND = '{"verdict": "beyond", "target": "全组的工资明细"}'


def _guard(
    contents: list[str] | None = None, failures: list[Exception | None] | None = None
) -> ScopeGuard:
    return ScopeGuard(
        ScriptedModelGateway(contents=contents or [WITHIN], failures=failures or []),
        model_alias="factory-summary",
    )


class TestScopeGuard:
    @pytest.mark.asyncio
    async def test_within_range_verdict_is_not_beyond(self) -> None:
        verdict = await _guard().check(
            question="我上个月的工资明细",
            capability_title="个人工资明细",
            role=Role.EMPLOYEE,
            logical_call_id="call-1",
        )

        assert verdict.beyond is False
        assert verdict.target == ""
        assert verdict.model_alias == "factory-summary"
        assert verdict.actual_model

    @pytest.mark.asyncio
    async def test_beyond_verdict_carries_a_sanitized_target(self) -> None:
        payload = '{"verdict": "beyond", "target": "全组的工资明细，可能包含\\n换行"}'
        verdict = await _guard([payload]).check(
            question="我想知道全组的工资明细",
            capability_title="个人工资明细",
            role=Role.EMPLOYEE,
            logical_call_id="call-1",
        )

        assert verdict.beyond is True
        assert "\n" not in verdict.target
        assert "全组的工资明细" in verdict.target

    @pytest.mark.asyncio
    async def test_fenced_json_output_is_accepted(self) -> None:
        fenced = "```json\n" + BEYOND + "\n```"
        verdict = await _guard([fenced]).check(
            question="全厂的工资",
            capability_title="个人工资明细",
            role=Role.EMPLOYEE,
            logical_call_id="call-1",
        )

        assert verdict.beyond is True
        assert verdict.repaired is False

    @pytest.mark.asyncio
    async def test_invalid_verdict_raises_structured_error(self) -> None:
        from factory_agent.application.structured import StructuredOutputError

        garbage = ["不是 JSON", "仍然不是 JSON"]
        with pytest.raises(StructuredOutputError):
            await _guard(garbage).check(
                question="全组的工资明细",
                capability_title="个人工资明细",
                role=Role.EMPLOYEE,
                logical_call_id="call-1",
            )

    @pytest.mark.asyncio
    async def test_gateway_failure_propagates(self) -> None:
        with pytest.raises(ModelGatewayError):
            await _guard(
                failures=[
                    ModelGatewayError(
                        ModelErrorCategory.UNAVAILABLE, "down", duration_ms=4
                    )
                ]
            ).check(
                question="全组的工资明细",
                capability_title="个人工资明细",
                role=Role.EMPLOYEE,
                logical_call_id="call-1",
            )

    @pytest.mark.asyncio
    async def test_prompt_carries_role_range_and_question_only(self) -> None:
        gateway = ScriptedModelGateway(contents=[WITHIN])
        guard = ScopeGuard(gateway, model_alias="factory-summary")
        await guard.check(
            question="我想知道全组的工资明细",
            capability_title="个人工资明细",
            role=Role.GROUP_LEADER,
            logical_call_id="call-1",
        )

        assert len(gateway.requests) == 1
        request = gateway.requests[0]
        assert request.stage.value == "scope_guard"
        assert request.json_output is True
        system_text = request.messages[0].content
        user_text = request.messages[-1].content
        assert system_text == SCOPE_GUARD_SYSTEM_PROMPT
        assert "01（组长）" in user_text
        assert "所绑定小组" in user_text
        assert "个人工资明细" in user_text
        assert "全组的工资明细" in user_text

    def test_deny_message_uses_the_authoritative_range_text(self) -> None:
        text = deny_message(Role.EMPLOYEE, "全组的工资明细")
        assert "没有权限" in text
        assert "全组的工资明细" in text
        assert "本人的产量与工资数据" in text

        unnamed = deny_message(Role.EMPLOYEE, None)
        assert "该范围的数据" in unnamed

    def test_deny_message_trims_an_overlong_target(self) -> None:
        text = deny_message(Role.EMPLOYEE, "x" * 200)
        assert "x" * 200 not in text

    def test_system_prompt_does_not_imply_owner_exceeds_scope_for_factory_wide(self) -> None:
        """The owner may ask about the whole factory; the rule list must not
        flag that as a beyond example (role ceiling is the whole tenant)."""
        assert "老板可问全厂" in SCOPE_GUARD_SYSTEM_PROMPT
        assert "或任何人问全厂" not in SCOPE_GUARD_SYSTEM_PROMPT
