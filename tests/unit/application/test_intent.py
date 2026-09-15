

import json
from datetime import datetime, timezone

import pytest

from factory_agent.application.intent import (
    REJECTED_SLOT_NAMES,
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
    clarification_for,
)
from factory_agent.domain import CapabilityId, Role
from tests.support.session import ScriptedModelGateway

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
TZ = "Asia/Shanghai"

CATALOG = CapabilityCatalog(
    specs=(
        CapabilitySpec(
            capability_id=CapabilityId("FR-001"),
            title="查看本人产量",
            required_slots=("time_range",),
        ),
        CapabilitySpec(
            capability_id=CapabilityId("FR-005"),
            title="查看订单进度",
            required_slots=("time_range", "order_codes"),
        ),
        CapabilitySpec(
            capability_id=CapabilityId("chitchat"),
            title="闲聊与常识问答",
            description="处理问候、寒暄以及与工厂业务无关的常识问答。",
            required_slots=(),
        ),
    )
)


def parser(gateway: ScriptedModelGateway) -> CapabilityIntentParser:
    return CapabilityIntentParser(gateway, CATALOG, model_alias="factory-fast", timezone_name=TZ)


def payload(**overrides: object) -> str:
    body: dict[str, object] = {
        "capability_id": "FR-001",
        "confidence": 0.92,
        "slots": {"time_expression": "上个月"},
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


@pytest.mark.asyncio
async def test_complete_intent_needs_no_clarification() -> None:
    gateway = ScriptedModelGateway(contents=[payload()])

    outcome = await parser(gateway).parse("上个月我做了多少件", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("FR-001")
    assert outcome.intent.needs_clarification is False
    assert outcome.clarification is None
    assert outcome.intent.slots.time_range_start is not None


@pytest.mark.asyncio
async def test_relative_time_uses_the_factory_timezone_and_injected_clock() -> None:
    gateway = ScriptedModelGateway(contents=[payload()])

    outcome = await parser(gateway).parse("上个月产量", now=NOW, logical_call_id="c1")

    assert outcome.intent.slots.time_range_start is not None
    assert outcome.intent.slots.time_range_start.isoformat() == "2026-06-30T16:00:00+00:00"


@pytest.mark.asyncio
async def test_bare_month_expression_fills_the_time_range_without_clarification() -> None:
    """\"我7月的工资明细\" must resolve to 2026-07 instead of asking a follow-up."""
    gateway = ScriptedModelGateway(contents=[payload(slots={"time_expression": "7月"})])

    outcome = await parser(gateway).parse("我7月的工资明细是多少", now=NOW, logical_call_id="c1")

    assert outcome.intent.needs_clarification is False
    assert outcome.clarification is None
    assert outcome.intent.slots.time_range_start is not None
    assert outcome.intent.slots.time_range_start.isoformat() == "2026-06-30T16:00:00+00:00"
    assert outcome.intent.slots.time_range_end.isoformat() == "2026-07-31T16:00:00+00:00"


@pytest.mark.asyncio
async def test_unregistered_capability_never_reaches_the_executor() -> None:
    gateway = ScriptedModelGateway(contents=[payload(capability_id="FR-999")])

    outcome = await parser(gateway).parse("随便查点什么", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id is None
    assert "capability" in outcome.intent.ambiguous
    assert outcome.clarification is not None


@pytest.mark.asyncio
async def test_low_confidence_asks_instead_of_running_a_heavy_query() -> None:
    gateway = ScriptedModelGateway(contents=[payload(confidence=0.2)])

    outcome = await parser(gateway).parse("看看情况", now=NOW, logical_call_id="c1")

    assert outcome.intent.needs_clarification is True
    assert "capability" in outcome.intent.ambiguous


@pytest.mark.asyncio
async def test_missing_required_slot_produces_a_short_clarification() -> None:
    gateway = ScriptedModelGateway(
        contents=[payload(capability_id="FR-005", slots={"time_expression": "上个月"})]
    )

    outcome = await parser(gateway).parse("订单进度", now=NOW, logical_call_id="c1")

    assert outcome.intent.missing == ("order_codes",)
    assert outcome.clarification == "请提供具体的订单号。"


@pytest.mark.asyncio
async def test_scope_identifiers_from_the_model_are_rejected() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_expression": "上个月",
                    "employee_ids": ["E-1", "E-2"],
                    "dept_ids": ["D-9"],
                    "sql": "select * from payroll",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("查所有人工资", now=NOW, logical_call_id="c1")

    assert set(outcome.rejected_slots) == {"dept_ids", "employee_ids", "sql"}
    assert not hasattr(outcome.intent.slots, "employee_ids")
    assert not hasattr(outcome.intent.slots, "dept_ids")


def test_rejected_slot_names_cover_scope_and_execution_escapes() -> None:
    assert {"employee_ids", "dept_ids", "sql", "url", "tenant_id"} <= REJECTED_SLOT_NAMES


@pytest.mark.asyncio
async def test_unparseable_time_phrase_becomes_ambiguity_not_a_guess() -> None:
    gateway = ScriptedModelGateway(contents=[payload(slots={"time_expression": "很久以前"})])

    outcome = await parser(gateway).parse("很久以前的产量", now=NOW, logical_call_id="c1")

    assert "time_range" in outcome.intent.ambiguous
    assert outcome.intent.slots.time_range_start is None


@pytest.mark.asyncio
async def test_code_lists_are_deduplicated_and_bounded() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                capability_id="FR-005",
                slots={
                    "time_expression": "上个月",
                    "order_codes": ["SO-1", "SO-1", *[f"SO-{index}" for index in range(50)]],
                },
            )
        ]
    )

    outcome = await parser(gateway).parse("订单进度", now=NOW, logical_call_id="c1")

    codes = outcome.intent.slots.order_codes
    assert len(codes) <= 20
    assert len(set(codes)) == len(codes)


@pytest.mark.asyncio
async def test_history_is_included_but_bounded() -> None:
    gateway = ScriptedModelGateway(contents=[payload()])

    await parser(gateway).parse("上个月产量", now=NOW, logical_call_id="c1")

    request = gateway.requests[0]
    assert request.json_output is True
    assert request.messages[0].role == "system"
    assert "FR-001" in request.messages[0].content


@pytest.mark.asyncio
async def test_empty_user_text_is_rejected_before_any_model_call() -> None:
    gateway = ScriptedModelGateway(contents=[payload()])

    with pytest.raises(Exception):
        await parser(gateway).parse("   ", now=NOW, logical_call_id="c1")

    assert gateway.requests == []


def test_clarification_is_none_for_a_complete_intent() -> None:
    from factory_agent.domain import CapabilityIntent, IntentSlots

    intent = CapabilityIntent(
        capability_id=CapabilityId("FR-001"),
        confidence=0.9,
        slots=IntentSlots(time_range_start=NOW, time_range_end=NOW),
    )

    assert clarification_for(intent) is None


@pytest.mark.asyncio
async def test_greeting_selects_the_reserved_chitchat_capability() -> None:
    gateway = ScriptedModelGateway(contents=[payload(capability_id="chitchat", slots={})])

    outcome = await parser(gateway).parse("你好", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("chitchat")
    assert outcome.intent.needs_clarification is False
    assert outcome.intent.missing == ()
    assert outcome.intent.ambiguous == ()
    assert outcome.clarification is None


@pytest.mark.asyncio
async def test_chitchat_with_low_confidence_is_not_treated_as_confirmed() -> None:
    gateway = ScriptedModelGateway(
        contents=[payload(capability_id="chitchat", confidence=0.2, slots={})]
    )

    outcome = await parser(gateway).parse("随便聊聊", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("chitchat")
    assert outcome.intent.needs_clarification is True
    assert "capability" in outcome.intent.ambiguous


@pytest.mark.asyncio
async def test_chitchat_type_carries_content_from_the_same_call() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                type="chitchat",
                capability_id="chitchat",
                slots={},
                content="你好呀！我是工厂助手。",
            )
        ]
    )

    outcome = await parser(gateway).parse("你好", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("chitchat")
    assert outcome.intent.needs_clarification is False
    assert outcome.content == "你好呀！我是工厂助手。"


@pytest.mark.asyncio
async def test_capability_type_never_accepts_chitchat_content() -> None:
    gateway = ScriptedModelGateway(contents=[payload(type="capability", content="不应出现")])

    outcome = await parser(gateway).parse("上个月产量", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("FR-001")
    assert outcome.content is None


@pytest.mark.asyncio
async def test_legacy_payload_without_type_keeps_chitchat_routing_and_content() -> None:
    gateway = ScriptedModelGateway(
        contents=[payload(capability_id="chitchat", slots={}, content="老格式仍带答案")]
    )

    outcome = await parser(gateway).parse("你好", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("chitchat")
    assert outcome.content == "老格式仍带答案"


@pytest.mark.asyncio
async def test_multi_turn_rewrite_query_is_captured_with_the_intent() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                type="capability",
                capability_id="FR-001",
                slots={"time_expression": "本月"},
                rewrite_query="查询我这个月的工资明细",
            )
        ]
    )

    outcome = await parser(gateway).parse("那这个月呢？", now=NOW, logical_call_id="c1")

    assert outcome.intent.capability_id == CapabilityId("FR-001")
    assert outcome.rewrite_query == "查询我这个月的工资明细"
    assert outcome.intent.slots.time_range_start is not None


def test_describe_lists_chinese_titles_descriptions_and_slot_labels() -> None:
    catalog = CapabilityCatalog(
        specs=(
            CapabilitySpec(
                capability_id=CapabilityId("fr001_personal_output"),
                title="个人产量统计",
                description="按日期、款号、工序统计本人的计件产量（合格件数，次品不计）。",
                required_slots=("time_range",),
            ),
            CapabilitySpec(
                capability_id=CapabilityId("chitchat"),
                title="闲聊与常识问答",
                description="处理问候、寒暄以及与工厂业务无关的常识问答。",
            ),
        )
    )

    text = catalog.describe()

    assert "- fr001_personal_output（个人产量统计）：按日期" in text
    assert "需要提供：时间范围" in text
    assert "- chitchat（闲聊与常识问答）" in text


@pytest.mark.asyncio
async def test_prompt_carries_the_clock_anchor_and_the_time_slot_keys() -> None:
    gateway = ScriptedModelGateway(contents=[payload()])

    await parser(gateway).parse("上个月产量", now=NOW, logical_call_id="c1")

    system = gateway.requests[0].messages[0].content
    assert "今天是 2026-08-24" in system
    assert "time_range_start" in system
    assert "time_range_end" in system


@pytest.mark.asyncio
async def test_naive_iso_range_is_localized_to_the_factory_timezone() -> None:
    """A model that omits the offset means factory local time, as the rules do."""
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_range_start": "2026-08-01",
                    "time_range_end": "2026-08-02",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("8月1日的产量", now=NOW, logical_call_id="c1")

    slots = outcome.intent.slots
    assert slots.time_range_start is not None
    assert slots.time_range_start.isoformat() == "2026-07-31T16:00:00+00:00"
    assert slots.time_range_end is not None
    assert slots.time_range_end.isoformat() == "2026-08-01T16:00:00+00:00"


@pytest.mark.asyncio
async def test_model_range_beyond_the_ceiling_falls_back_to_the_rule_layer() -> None:
    """A hallucinated two-year span for \"本月\" must not become a rejection."""
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_expression": "本月",
                    "time_range_start": "2025-01-01T00:00:00+00:00",
                    "time_range_end": "2027-01-01T00:00:00+00:00",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("本月产量", now=NOW, logical_call_id="c1")

    assert "time_range" not in outcome.intent.ambiguous
    assert outcome.intent.needs_clarification is False
    slots = outcome.intent.slots
    assert slots.time_range_start is not None
    assert slots.time_range_start.isoformat() == "2026-07-31T16:00:00+00:00"
    assert slots.time_range_end is not None
    assert slots.time_range_end.isoformat() == "2026-08-31T16:00:00+00:00"


@pytest.mark.asyncio
async def test_model_range_starting_past_tomorrow_is_discarded() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_expression": "本月",
                    "time_range_start": "2028-01-01T00:00:00+00:00",
                    "time_range_end": "2028-02-01T00:00:00+00:00",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("本月产量", now=NOW, logical_call_id="c1")

    assert outcome.intent.slots.time_range_start is not None
    assert outcome.intent.slots.time_range_start.isoformat() == "2026-07-31T16:00:00+00:00"


@pytest.mark.asyncio
async def test_an_explicit_wide_range_survives_for_the_ceiling_gate() -> None:
    """超一年的显式区间要走会话上限提示，而不是被降级成一次追问。"""
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_range_start": "2025-01-01T00:00:00+00:00",
                    "time_range_end": "2026-08-01T00:00:00+00:00",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("近两年的产量", now=NOW, logical_call_id="c1")

    slots = outcome.intent.slots
    assert outcome.intent.needs_clarification is False
    assert slots.time_range_start is not None
    assert slots.time_range_start.isoformat() == "2025-01-01T00:00:00+00:00"
    assert slots.time_range_end is not None
    assert slots.time_range_end.isoformat() == "2026-08-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_rejected_model_range_without_a_rule_layer_phrase_asks_instead() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_expression": "很久以前",
                    "time_range_start": "2028-01-01T00:00:00+00:00",
                    "time_range_end": "2028-02-01T00:00:00+00:00",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("很久以前的产量", now=NOW, logical_call_id="c1")

    assert "time_range" in outcome.intent.ambiguous
    assert outcome.intent.slots.time_range_start is None


@pytest.mark.asyncio
async def test_rule_primary_mode_ignores_the_model_range() -> None:
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_expression": "本月",
                    "time_range_start": "2026-07-01T00:00:00+00:00",
                    "time_range_end": "2026-07-02T00:00:00+00:00",
                }
            )
        ]
    )
    rule_parser = CapabilityIntentParser(
        gateway,
        CATALOG,
        model_alias="factory-fast",
        timezone_name=TZ,
        time_parse_mode="rule_primary",
    )

    outcome = await rule_parser.parse("本月产量", now=NOW, logical_call_id="c1")

    slots = outcome.intent.slots
    assert slots.time_range_start is not None
    assert slots.time_range_start.isoformat() == "2026-07-31T16:00:00+00:00"
    assert slots.time_range_end is not None
    assert slots.time_range_end.isoformat() == "2026-08-31T16:00:00+00:00"


@pytest.mark.asyncio
async def test_an_inverted_model_range_is_flagged_and_never_used() -> None:
    """\"从8月10日到8月1日\" has no usable span: ask, and say why."""
    gateway = ScriptedModelGateway(
        contents=[
            payload(
                slots={
                    "time_range_start": "2026-08-10T00:00:00+08:00",
                    "time_range_end": "2026-08-01T00:00:00+08:00",
                }
            )
        ]
    )

    outcome = await parser(gateway).parse("从8月10日到8月1日", now=NOW, logical_call_id="c1")

    assert outcome.intent.slots.time_range_start is None
    assert outcome.intent.slots.time_range_end is None
    assert "time_range" in outcome.intent.ambiguous
    assert outcome.clarification is not None


def guarding_parser(
    gateway: ScriptedModelGateway, *, mode: str = "merged"
) -> CapabilityIntentParser:
    return CapabilityIntentParser(
        gateway,
        CATALOG,
        model_alias="factory-fast",
        timezone_name=TZ,
        scope_guard_mode="dedicated" if mode == "dedicated" else "merged",
    )


@pytest.mark.asyncio
async def test_merged_mode_asks_for_the_scope_verdict_with_the_caller_role() -> None:
    gateway = ScriptedModelGateway(contents=[payload()])

    await guarding_parser(gateway).parse(
        "全组的工资明细", now=NOW, logical_call_id="c1", role=Role.EMPLOYEE
    )

    system_text = gateway.requests[0].messages[0].content
    assert "权限范围判定" in system_text
    assert "00（普通员工）" in system_text
    assert "本人的产量与工资数据" in system_text
    assert '"scope"' in system_text


@pytest.mark.asyncio
async def test_merged_mode_reads_the_scope_key() -> None:
    scope = {"verdict": "beyond", "target": "全组的工资明细"}
    gateway = ScriptedModelGateway(contents=[payload(scope=scope)])

    outcome = await guarding_parser(gateway).parse(
        "全组的工资明细", now=NOW, logical_call_id="c1", role=Role.EMPLOYEE
    )

    assert outcome.includes_scope is True
    assert outcome.scope_verdict is not None
    assert outcome.scope_verdict.beyond is True
    assert outcome.scope_verdict.target == "全组的工资明细"


@pytest.mark.asyncio
async def test_merged_mode_without_a_usable_scope_key_reports_no_verdict() -> None:
    """A missing or invalid key must leave the dedicated call in charge."""
    gateway = ScriptedModelGateway(contents=[payload(scope={"verdict": "也许"})])

    outcome = await guarding_parser(gateway).parse(
        "上个月产量", now=NOW, logical_call_id="c1", role=Role.EMPLOYEE
    )

    assert outcome.includes_scope is True
    assert outcome.scope_verdict is None


@pytest.mark.asyncio
async def test_merged_mode_without_a_role_asks_for_nothing_extra() -> None:
    """The rules are role-relative, so an unknown role means no scope section."""
    scope = {"verdict": "beyond", "target": "全组"}
    gateway = ScriptedModelGateway(contents=[payload(scope=scope)])

    outcome = await guarding_parser(gateway).parse("上个月产量", now=NOW, logical_call_id="c1")

    assert "权限范围判定" not in gateway.requests[0].messages[0].content
    assert outcome.includes_scope is False
    assert outcome.scope_verdict is None


@pytest.mark.asyncio
async def test_dedicated_mode_ignores_the_scope_key_entirely() -> None:
    """``dedicated`` must reproduce today's prompt and today's metering shape."""
    scope = {"verdict": "beyond", "target": "全组"}
    gateway = ScriptedModelGateway(contents=[payload(scope=scope)])

    outcome = await guarding_parser(gateway, mode="dedicated").parse(
        "上个月产量", now=NOW, logical_call_id="c1", role=Role.EMPLOYEE
    )

    assert "权限范围判定" not in gateway.requests[0].messages[0].content
    assert outcome.includes_scope is False
    assert outcome.scope_verdict is None
