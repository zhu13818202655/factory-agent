import json
from datetime import UTC, date, datetime, timedelta
from typing import cast

import httpx
import pytest

from factory_agent.data_api.verify_analysis_sources import (
    SourceVerification,
    VerificationSettings,
    verify_sources,
)
from factory_agent.observability.debug_trace import debug_capture_enabled
from factory_agent.source_verification import FactoryVerificationPolicy
from tests.unit.data_api.test_knowledge_directory import (
    hanging_line_payload as _hanging_line,
)
from tests.unit.data_api.test_knowledge_directory import (
    production_type_payload as _type_directory,
)
from tests.unit.data_api.test_knowledge_directory import (
    style_directory_payload as _style_directory,
)
from tests.unit.data_api.test_knowledge_directory import (
    style_form_payload as _form,
)
from tests.unit.data_api.test_knowledge_directory import (
    style_process_payload as _process,
)


def _settings() -> VerificationSettings:
    return VerificationSettings.model_validate(
        {
            "FACTORY_AGENT_CANONICAL_MES_BASE_URL": "http://synthetic.invalid",
            "FACTORY_AGENT_MES_USER_CREDENTIAL_99": "PRIVATE_ENCRYPTED_CANARY",
        }
    )


class _SilentLogger:
    def info(self, *args: object, **kwargs: object) -> None:
        pass

    def warning(self, *args: object, **kwargs: object) -> None:
        pass


@pytest.fixture(autouse=True)
def verification_logger_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("factory_agent.data_api.token_gateway._LOGGER", _SilentLogger())
    monkeypatch.setattr("factory_agent.data_api.hongzhao._LOGGER", _SilentLogger())


def _token(role: str) -> dict[str, object]:
    return {
        "tokenType": "Bearer",
        "accessToken": "PRIVATE_ACCESS_CANARY",
        "expiresIn": 7200,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "user": "PRIVATE_EMPLOYEE_CANARY",
        "uname": "PRIVATE_NAME_CANARY",
        "dept": "PRIVATE_DEPARTMENT_CANARY",
        "appkey": "PRIVATE_TENANT_CANARY",
        "sign": "PRIVATE_SIGNATURE_CANARY",
        "timestamp": 1,
        "roles": [role],
    }


def _respond(role: str, calls: list[str], *, fail_token: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert debug_capture_enabled() is False
        path = request.url.path
        calls.append(path)
        if path.endswith("/token"):
            if fail_token:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "PRIVATE_UPSTREAM_ERROR_CANARY",
                        "timestamp": 1,
                        "result": None,
                    },
                )
            result: object = _token(role)
        elif path.endswith("/HuohaoQuery"):
            result = _style_directory()
            cast(list[dict[str, object]], result["huohaoList"])[0]["bbreed"] = "STYLE-0001"
        elif path.endswith("/HuohaoFormQuery"):
            result = _form()
        elif path.endswith("/ScTypeQuery"):
            result = _type_directory()
        elif path.endswith("/RfidWorktypeQuery"):
            result = {
                "worktypeList": [
                    {
                        "bh": "PROCESS-A",
                        "name": "synthetic process",
                        "gxtype": 0,
                        "isdelete": 0,
                        "section": "stage",
                    }
                ],
                "total": 1,
            }
        elif path.endswith("/HuohaoWorktypeQuery"):
            result = {"list": [_process()], "total": 1}
        elif path.endswith("/DgZuGridPageList"):
            result = {"list": [_hanging_line(1)], "total": 1}
        elif path.endswith("/GongziClQuery"):
            body = cast(dict[str, object], json.loads(request.content))
            assert body["Flag"] == "0"
            result = {
                "list": [
                    {
                        "inputdate": "2026-09-01",
                        "chuanghao": "SYNTHETIC-BED",
                        "huohao": "0001",
                        "bbreed": "STYLE-0001",
                        "worktype": "PROCESS-A",
                        "sl": 1,
                        "uid": "PRIVATE_EMPLOYEE_CANARY",
                        "je": "PRIVATE_PAY_CANARY",
                        "dept": "PRIVATE_DEPARTMENT_CANARY",
                    }
                ],
                "total": 1,
            }
        elif path.endswith("/ScjdQuery"):
            result = {
                "list": [
                    {
                        "dh": "PRIVATE_BILL_CANARY",
                        "rq": "2026-09-01",
                        "chuanghao": "SYNTHETIC-BED",
                        "huohao": "0001",
                        "bbreed": "STYLE-0001",
                        "worktype": "PROCESS-A",
                        "fhsl": 1,
                        "sssl": 0,
                        "dept": "PRIVATE_DEPARTMENT_CANARY",
                    }
                ],
                "total": 1,
            }
        elif path.endswith("/SclzdWorktypeQuery"):
            body = cast(dict[str, object], json.loads(request.content))
            assert body["dh"] == "PRIVATE_BILL_CANARY"
            result = {"list": [{**_process(), "dh": "PRIVATE_BILL_CANARY"}], "total": 1}
        else:
            raise AssertionError("unexpected source operation")
        assert request.method == "POST"
        return httpx.Response(
            200, json={"code": 1, "message": "ok", "timestamp": 1, "result": result}
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_test_verification_emits_only_safe_metadata_and_real_scope_before_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    emitted: list[SourceVerification] = []
    capture_was_enabled = debug_capture_enabled()
    results = await verify_sources(
        _settings(),
        date(2026, 9, 1),
        policy=FactoryVerificationPolicy(),
        transport=_respond("99", calls),
        emit=emitted.append,
    )
    assert len(results) == len(emitted) == 12
    assert all(result.complete and result.category == "validated" for result in results)
    assert calls[0].endswith("/token")
    assert sum(path.endswith("/token") for path in calls) == 1
    assert sum(path.endswith("/GongziClQuery") for path in calls) == 1
    assert sum(path.endswith("/ScjdQuery") for path in calls) == 1
    assert debug_capture_enabled() is capture_was_enabled
    captured = capsys.readouterr()
    assert "PRIVATE_" not in captured.out + captured.err + repr(results)
    assert all(item.missing_fields == () for item in results)


@pytest.mark.asyncio
async def test_employee_probe_cannot_fetch_progress_or_invent_authorized_bill() -> None:
    calls: list[str] = []
    results = await verify_sources(
        _settings(),
        date(2026, 9, 1),
        policy=FactoryVerificationPolicy(),
        transport=_respond("00", calls),
    )
    by_dataset = {item.dataset_id: item for item in results}
    assert by_dataset["reported_production"].complete
    assert by_dataset["production_progress"].category == "forbidden"
    assert by_dataset["bill_processes"].category == "no_validated_entity"
    assert not any(
        path.endswith("/ScjdQuery") or path.endswith("/SclzdWorktypeQuery") for path in calls
    )


@pytest.mark.asyncio
async def test_rejected_test_exchange_reports_category_and_zero_business_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    results = await verify_sources(
        _settings(),
        date(2026, 9, 1),
        policy=FactoryVerificationPolicy(),
        transport=_respond("99", calls, fail_token=True),
    )
    assert len(results) == 1 and results[0].dataset_id == "authorization"
    assert results[0].category == "upstream_invalid"
    assert len(calls) == 1 and calls[0].endswith("/token")
    captured = capsys.readouterr()
    assert "PRIVATE_UPSTREAM_ERROR_CANARY" not in captured.out + captured.err + repr(results)
