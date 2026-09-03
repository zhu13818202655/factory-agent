"""Role-consistency safety net — session pipeline and kernel regression tests.

Proves the Story 2 dispositions end to end:
1. Exact hit (production/strict): the offending rows never reach the user
   visible result — the interaction terminates with ``scope_violation_exact``,
   a friendly prompt, a review record, and an audit alert.
2. Heuristic hit (production): rows are shown, the finding is logged with
   structured fields and never blocks.
3. Zero false positives on the real kernel over the Mock MES: a worker's
   personal wage and a boss's factory-wide ranking are never flagged, and the
   observed-ownership channel is populated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.customer import sign_of

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.business_filters import (
    BusinessFilterResolver,
    DeptRecord,
    EmployeeRecord,
)
from factory_agent.application.consistency import ConsistencyValidator
from factory_agent.application.filters import FilterNarrower
from factory_agent.application.intent import (
    CapabilityCatalog,
    CapabilityIntentParser,
    CapabilitySpec,
)
from factory_agent.application.permission_matrix import Capability
from factory_agent.application.session import SessionService, StartRequest
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.domain import (
    CapabilityId,
    DataScope,
    DeptId,
    EmployeeId,
    ExpectedRange,
    InteractionStatus,
    Role,
    ScopeVersion,
    SessionId,
    TenantContext,
    TenantId,
    TimeRange,
    UserId,
)
from factory_agent.execution.executor import ScopedExecutor
from factory_agent.execution.kernel import KernelCapabilityRunner, KernelSettings
from factory_agent.execution.recipes import load_recipes
from factory_agent.execution.result_table import default_metric_registry
from factory_agent.observability.audit import AuditEventType, InMemoryAuditSink
from factory_agent.ports import CapabilityRunRequest, CapabilityRunResult
from factory_agent.ports.contracts import TrustedCredential
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.scope_violation import InMemoryScopeViolationStore
from tests.support.session import (
    FrozenClock,
    InMemoryInteractionStore,
    RecordingCapabilityRunner,
    ScriptedModelGateway,
    SequentialIds,
)

NOW = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
RANGE = TimeRange(
    start=datetime(2026, 8, 1, tzinfo=timezone.utc),
    end=datetime(2026, 8, 31, tzinfo=timezone.utc),
)
SESSION = SessionId("session-consistency")


class ViolatingRunner(RecordingCapabilityRunner):
    """Returns a personal-wage result whose observed uid includes another employee."""

    def __init__(self, *, observed_uids: tuple[str, ...] = ("emp-1", "emp-2")) -> None:
        super().__init__()
        self._uids = observed_uids

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult:
        self.requests.append(request)
        return CapabilityRunResult(
            capability_id=request.capability_id,
            column_names=("gross_total",),
            rows=((Decimal("1"),),),
            observed_uid_values=self._uids,
            api_call_count=1,
            duration_ms=7,
        )


def _credential() -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId("tenant-a"), user_id=UserId("user-a"))


def _authorization() -> AuthorizationService:
    member = membership("user-a", "tenant-a", "emp-1", Role.EMPLOYEE)
    return AuthorizationService(
        memberships=FakeMembershipSource(
            memberships_by_credential={("tenant-a", "user-a"): member}
        ),
        organizations=FakeOrganizationSource(depts_by_employee={"emp-1": ("dept-1",)}),
        versions=FixedScopeVersionAssigner(),
    )


class _Directory:
    async def list_depts(self, scope: DataScope) -> tuple[DeptRecord, ...]:
        return (DeptRecord("dept-1", "一车间", "YCJ"),)

    async def list_employees(self, scope: DataScope) -> tuple[EmployeeRecord, ...]:
        return (EmployeeRecord("emp-1", "模拟员工甲", "MNYGJ"),)


def _build(
    *,
    role: Role = Role.EMPLOYEE,
    validation_mode: str = "production",
    runner: RecordingCapabilityRunner | None = None,
    enable_validator: bool = True,
) -> tuple[
    SessionService,
    InMemoryInteractionStore,
    InMemoryScopeViolationStore,
    InMemoryAuditSink,
]:
    catalog = CapabilityCatalog(
        specs=(
            CapabilitySpec(
                capability_id=CapabilityId("FR-002"),
                title="个人工资汇总",
                required_slots=("time_range",),
            ),
        )
    )
    payload = (
        '{"capability_id": "FR-002", "confidence": 0.95, "slots": {"time_expression": "上个月"}}'
    )
    gateway = ScriptedModelGateway(contents=[payload])
    parser = CapabilityIntentParser(
        gateway, catalog, model_alias="factory-fast", timezone_name="Asia/Shanghai"
    )
    store = InMemoryInteractionStore()
    violations = InMemoryScopeViolationStore()
    audit = InMemoryAuditSink()
    resolved_runner = runner or ViolatingRunner()
    service = SessionService(
        store,
        _authorization(),
        parser,
        resolved_runner,
        FrozenClock(NOW),
        new_id=SequentialIds(prefix="id"),
        limits=None,
        sleep=_async_noop,
        business_filters=BusinessFilterResolver(_Directory()),
        validator=ConsistencyValidator() if enable_validator else None,
        violations=violations,
        audit=audit,
        validation_mode=validation_mode,
    )
    return service, store, violations, audit


async def _async_noop(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_exact_hit_in_production_blocks_rows_and_alerts() -> None:
    service, store, violations, audit = _build(validation_mode="production")
    record = await service.start(_credential(), StartRequest(session_id=SESSION, text="上个月工资"))

    events = [
        event
        async for event in service.stream(_credential(), record.interaction_id, after_sequence=0)
    ]

    # The offending rows never surface as a result.
    names = [event.name for event in events]
    assert "interaction.result" not in names
    terminal = store.interactions[str(record.interaction_id)]
    assert terminal.status is InteractionStatus.FAILED
    assert terminal.error_category == "scope_violation_exact"
    error_messages = [m for m in store.messages if m.kind.value == "error"]
    assert len(error_messages) == 1
    assert "可查范围" in error_messages[0].text
    assert len(violations.records) == 1
    assert violations.records[0].level == "exact_hit"
    # Real-time alert carrier: the audit event.
    types = [event.event_type for event in audit.events]
    assert AuditEventType.SCOPE_VIOLATION_EXACT in types
    # No raw out-of-range uid on the record surface.
    assert "emp-2" not in violations.records[0].actual_summary


@pytest.mark.asyncio
async def test_exact_hit_in_strict_mode_blocks_with_integration_category() -> None:
    service, store, violations, _ = _build(validation_mode="strict")
    record = await service.start(_credential(), StartRequest(session_id=SESSION, text="上个月工资"))

    events = [
        event
        async for event in service.stream(_credential(), record.interaction_id, after_sequence=0)
    ]
    del events
    terminal = store.interactions[str(record.interaction_id)]
    assert terminal.status is InteractionStatus.FAILED
    assert terminal.error_category == "scope_violation_exact"
    assert len(violations.records) == 1
    assert violations.records[0].mode == "strict"


class MultiRowRunner(RecordingCapabilityRunner):
    """Single-subject summary returning multiple rows (heuristic shape)."""

    async def run(self, request: CapabilityRunRequest) -> CapabilityRunResult:
        self.requests.append(request)
        return CapabilityRunResult(
            capability_id=request.capability_id,
            column_names=("gross_total",),
            rows=((Decimal("1"),), (Decimal("2"),)),
            observed_uid_values=("emp-1",),
            api_call_count=1,
            duration_ms=7,
        )


@pytest.mark.asyncio
async def test_heuristic_hit_in_production_logs_and_keeps_the_result() -> None:
    service, store, violations, audit = _build(
        validation_mode="production", runner=MultiRowRunner()
    )
    record = await service.start(_credential(), StartRequest(session_id=SESSION, text="上个月工资"))

    events = [
        event
        async for event in service.stream(_credential(), record.interaction_id, after_sequence=0)
    ]

    result = next(event for event in events if event.name == "interaction.result")
    consistency = cast(dict[str, object], result.data.get("consistency"))
    assert consistency is not None
    assert consistency["level"] == "heuristic_hit"
    assert consistency["blocked"] is False
    terminal = store.interactions[str(record.interaction_id)]
    assert terminal.status is InteractionStatus.COMPLETED
    assert len(violations.records) == 1
    assert violations.records[0].level == "heuristic_hit"
    assert AuditEventType.SCOPE_VIOLATION_HEURISTIC in {event.event_type for event in audit.events}


@pytest.mark.asyncio
async def test_heuristic_hit_in_strict_mode_blocks() -> None:
    service, store, _, _ = _build(validation_mode="strict", runner=MultiRowRunner())
    record = await service.start(_credential(), StartRequest(session_id=SESSION, text="上个月工资"))

    events = [
        event
        async for event in service.stream(_credential(), record.interaction_id, after_sequence=0)
    ]
    del events
    terminal = store.interactions[str(record.interaction_id)]
    assert terminal.status is InteractionStatus.FAILED
    assert terminal.error_category == "scope_violation_heuristic"


@pytest.mark.asyncio
async def test_normal_result_is_not_flagged_when_validator_is_absent() -> None:
    """Default (validator unwired) keeps the pre-Story-2 behavior."""
    service, store, violations, _ = _build(validation_mode="production", enable_validator=False)
    record = await service.start(_credential(), StartRequest(session_id=SESSION, text="上个月工资"))

    events = [
        event
        async for event in service.stream(_credential(), record.interaction_id, after_sequence=0)
    ]
    assert any(event.name == "interaction.result" for event in events)
    assert store.interactions[str(record.interaction_id)].status is InteractionStatus.COMPLETED
    assert violations.records == []


def _bundle(user: str) -> MesCredentialBundle:
    timestamp = int(datetime.now(timezone.utc).timestamp())
    return MesCredentialBundle(
        access_token=f"MOCK-TOKEN-{user}",
        app_key="APPKEY-A",
        sign=sign_of("APPKEY-A", timestamp),
        timestamp=timestamp,
        expires_at=datetime.now(timezone.utc),
        user=UserId(user),
        uname="模拟",
    )


def _scope(employee: str, dept: str, *, mes_filtered: bool = True) -> DataScope:
    return DataScope(
        tenant_id=TenantId("APPKEY-A"),
        employee_ids=frozenset({EmployeeId(employee)}),
        dept_ids=frozenset({DeptId(dept)}),
        evaluated_at=NOW,
        scope_version=ScopeVersion("v1"),
        mes_filtered=mes_filtered,
    )


@pytest.mark.asyncio
async def test_zero_false_positive_worker_personal_wage_on_mock(mock_mes_app: Any) -> None:
    """00 员工查本人工资明细：真实内核+MES 返回自属行，校验零误报."""
    client = AsyncClient(transport=ASGITransport(app=mock_mes_app), base_url="http://test")
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter("http://test", _bundle("01001"), catalog, client=client)
    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(catalog.operation_ids),
        default_metric_registry(),
        settings=KernelSettings(page_size=2000, max_api_calls=300),
        clock=lambda: NOW,
    )
    try:
        result = await runner.run(
            CapabilityRunRequest(
                capability_id=CapabilityId("fr003_personal_wage_detail"),
                filters=FilterNarrower().narrow(
                    _scope("01001", "dept-a1"),
                    employee_ids=frozenset({EmployeeId("01001")}),
                ),
                time_range=RANGE,
                role=Role.EMPLOYEE,
            )
        )
    finally:
        await adapter.aclose()
        await client.aclose()

    # The observed-ownership channel is populated with the caller's own uid only.
    assert result.observed_uid_values == ("01001",)
    assert set(result.observed_dept_values) <= {"dept-a1"}
    context = TenantContext(
        tenant_id=TenantId("APPKEY-A"),
        user_id=UserId("01001"),
        employee_id=EmployeeId("01001"),
        role=Role.EMPLOYEE,
        resolved_at=NOW,
    )
    expected = ExpectedRange.from_context(context, _scope("01001", "dept-a1"))
    verdict = ConsistencyValidator().validate(
        result=result,
        capability=Capability.OWN_PAYROLL_DETAIL,
        expected=expected,
    )
    assert verdict.ok


@pytest.mark.asyncio
async def test_zero_false_positive_boss_factory_ranking_on_mock(mock_mes_app: Any) -> None:
    """99 老板全厂排名：角色无上限，永不误报."""
    client = AsyncClient(transport=ASGITransport(app=mock_mes_app), base_url="http://test")
    catalog = load_catalog()
    adapter = HongzhaoMesAdapter("http://test", _bundle("01009"), catalog, client=client)
    executor = ScopedExecutor(adapter=adapter, catalog=catalog)
    runner = KernelCapabilityRunner(
        executor,
        load_recipes(catalog.operation_ids),
        default_metric_registry(),
        settings=KernelSettings(page_size=2000, max_api_calls=300),
        clock=lambda: NOW,
    )
    try:
        result = await runner.run(
            CapabilityRunRequest(
                capability_id=CapabilityId("fr008_payroll_ranking"),
                filters=FilterNarrower().narrow(_scope("01009", "dept-a1", mes_filtered=True)),
                time_range=RANGE,
                role=Role.OWNER,
            )
        )
    finally:
        await adapter.aclose()
        await client.aclose()

    assert result.observed_uid_values  # ranking rows are observed
    context = TenantContext(
        tenant_id=TenantId("APPKEY-A"),
        user_id=UserId("01009"),
        employee_id=EmployeeId("01009"),
        role=Role.OWNER,
        resolved_at=NOW,
    )
    expected = ExpectedRange.from_context(context, _scope("01009", "dept-a1"))
    verdict = ConsistencyValidator().validate(
        result=result,
        capability=Capability.TEAM_PAYROLL_LIST,
        expected=expected,
    )
    assert verdict.ok
