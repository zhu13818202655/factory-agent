import argparse
import asyncio
import csv
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from scripts.run_qa_batch import (
    Case,
    Options,
    ProtocolError,
    Result,
    SecretFilter,
    load_cases,
    parse_events,
    role_credentials,
    run_batch,
    run_case,
    summarize,
    write_result,
)


def case(**updates: Any) -> Case:
    return Case.model_validate(
        {"case_id": "q1", "role": "员工", "question": "我上个月的产量是多少？", **updates}
    )


def frame(sequence: int, name: str, data: dict[str, Any]) -> str:
    return f"id: {sequence}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def result_frame(sequence: int = 1) -> str:
    return frame(
        sequence,
        "interaction.result",
        {
            "capability_id": "FR-001",
            "row_count": 1,
            "answer": "上个月产量为 12 件·工序。",
            "incomplete": False,
            "cache_source": {"sources": ["query"]},
        },
    )


def complete_frame(sequence: int = 2) -> str:
    return frame(sequence, "interaction.completed", {"status": "completed"})


def test_two_column_csv_and_role_codes_preserve_question_text(tmp_path: Path) -> None:
    path = tmp_path / "questions.csv"
    path.write_text(
        '\ufeffrole,question\n00,"上个月的产量,是多少？"\n99,本月全厂工资\n', encoding="utf-8"
    )
    cases = load_cases(path)
    assert [(item.case_id, item.role, item.question) for item in cases] == [
        ("q0001", "员工", "上个月的产量,是多少？"),
        ("q0002", "老板", "本月全厂工资"),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "role,question\n平台管理员,本月产量\n",
        "role,question\n员工,\n",
        "role,question\n员工,产量,多余字段\n",
        "case_id,role,question\nq1,员工,产量\nq1,老板,工资\n",
        "role,question,expected_outcome\n员工,产量,correct\n",
    ],
)
def test_invalid_cases_are_rejected_before_requests(tmp_path: Path, source: str) -> None:
    path = tmp_path / "questions.csv"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


def test_credentials_support_both_names_without_role_override() -> None:
    credentials = role_credentials(
        {
            "MES_USER_CREDENTIAL_00": "synthetic-employee",
            "FACTORY_AGENT_MES_USER_CREDENTIAL_01": "synthetic-leader",
            "FACTORY_AGENT_MES_USER_CREDENTIAL_99": '["synthetic-owner"]',
        }
    )
    assert set(credentials) == {"员工", "组长", "老板"}
    assert credentials["员工"].get_secret_value() == "synthetic-employee"
    assert "synthetic-employee" not in repr(credentials)


def test_multiple_owner_credentials_are_not_arbitrarily_selected() -> None:
    assert role_credentials({"FACTORY_AGENT_MES_USER_CREDENTIAL_99": '["a", "b"]'}) == {}


@pytest.mark.asyncio
async def test_sse_accepts_comments_and_multiline_json() -> None:
    async def lines() -> AsyncIterator[str]:
        for line in [
            ": heartbeat",
            "",
            "id: 3",
            "event: interaction.answer",
            'data: {"text":',
            'data: "你好"}',
            "",
        ]:
            yield line

    events = [event async for event in parse_events(lines())]
    assert len(events) == 1
    assert events[0].sequence == 3
    assert events[0].data == {"text": "你好"}


@pytest.mark.asyncio
async def test_sse_rejects_malformed_payload_without_exposing_it() -> None:
    async def lines() -> AsyncIterator[str]:
        for line in ["id: 1", "event: interaction.result", "data: CANARY-MALFORMED-SECRET", ""]:
            yield line

    with pytest.raises(ProtocolError, match="invalid_event") as captured:
        _ = [event async for event in parse_events(lines())]
    assert "CANARY" not in str(captured.value)


@pytest.mark.asyncio
async def test_question_uses_only_authenticated_api_and_saves_grounded_answer() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Factory-Credential"] == "synthetic-employee"
        if request.method == "POST":
            assert json.loads(request.content) == {"text": "我上个月的产量是多少？"}
            return httpx.Response(201, json={"interaction_id": "it1"})
        if request.url.path.endswith("/trace"):
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=result_frame() + complete_frame(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test.invalid"
    ) as client:
        result = await run_case(
            client,
            case(expected_capability="FR-001", expected_outcome="answer|empty"),
            SecretStr("synthetic-employee"),
            Options("http://test.invalid"),
            round_number=1,
            session_id="s1",
        )
    assert result.status == "answer"
    assert result.answer == "上个月产量为 12 件·工序。"
    assert result.row_count == 1
    assert result.expectation_match is True
    assert result.cache_source == {"sources": ["query"]}
    assert result.trace_status == "http_503"
    assert result.observed_mes_spans is None
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_missing_credential_makes_zero_api_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("missing credentials must never issue a request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test.invalid"
    ) as client:
        result = await run_case(
            client, case(), None, Options("http://test.invalid"), round_number=1, session_id="s1"
        )
    assert result.status == "skipped"
    assert result.error_category == "credential_not_configured"
    assert result.expectation_match is None


@pytest.mark.asyncio
async def test_stream_resume_keeps_one_interaction_and_deduplicates_events() -> None:
    starts, streams = 0, 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts, streams
        if request.method == "POST":
            starts += 1
            return httpx.Response(201, json={"interaction_id": "it1"})
        if request.url.path.endswith("/trace"):
            return httpx.Response(503)
        streams += 1
        if streams == 1:
            assert request.headers["Last-Event-ID"] == "0"
            text = result_frame()
        else:
            assert request.headers["Last-Event-ID"] == "1"
            text = result_frame() + complete_frame()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test.invalid"
    ) as client:
        result = await run_case(
            client,
            case(),
            SecretStr("synthetic-employee"),
            Options("http://test.invalid"),
            round_number=1,
            session_id="s1",
        )
    assert (starts, streams) == (1, 2)
    assert result.events.count("interaction.result") == 1
    assert result.status == "answer"


@pytest.mark.parametrize(
    ("event_name", "payload", "status"),
    [
        ("interaction.clarification", {"question": "请补充订单。"}, "clarification"),
        (
            "interaction.failed",
            {"error_category": "scope_forbidden", "message": "范围不足"},
            "denied",
        ),
        ("interaction.failed", {"error_category": "upstream_timeout"}, "failed"),
        ("interaction.failed", {"error_category": "time_range_exceeds_limit"}, "rejected"),
        ("interaction.cancelled", {"error_category": "cancelled"}, "cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_outcomes_are_not_collapsed_into_success(
    event_name: str, payload: dict[str, Any], status: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"interaction_id": "it1"})
        if request.url.path.endswith("/trace"):
            return httpx.Response(503)
        stream = frame(1, event_name, payload)
        if event_name not in {"interaction.failed", "interaction.cancelled"}:
            stream += complete_frame()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test.invalid"
    ) as client:
        result = await run_case(
            client,
            case(),
            SecretStr("synthetic-employee"),
            Options("http://test.invalid"),
            round_number=1,
            session_id="s1",
        )
    assert result.status == status


class HangingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b": heartbeat\n\n"
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_timeout_cancels_the_created_run() -> None:
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            cancelled.append(request.url.path)
            return httpx.Response(200)
        if request.method == "POST":
            return httpx.Response(201, json={"interaction_id": "it1"})
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=HangingStream()
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test.invalid"
    ) as client:
        result = await run_case(
            client,
            case(),
            SecretStr("synthetic-employee"),
            Options("http://test.invalid", timeout_seconds=0.02),
            round_number=1,
            session_id="s1",
        )
    assert result.error_category == "client_timeout"
    assert result.status == "failed"
    assert cancelled == ["/v1/interactions/it1/cancel"]


def test_result_files_hide_credentials_and_escape_csv_formulas(tmp_path: Path) -> None:
    credential = "CANARY-CREDENTIAL"
    result = Result("q1", "员工", "=1+1", 1, answer=f"{credential} 产量 12", status="answer")
    redactor = SecretFilter({"MES_USER_CREDENTIAL_00": credential})
    write_result(tmp_path, result, redactor)
    write_result(tmp_path, result, redactor)
    for name in ("results.csv", "results.jsonl"):
        assert credential not in (tmp_path / name).read_text(encoding="utf-8-sig")
    with (tmp_path / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["question"] == "'=1+1"
    docs = [
        json.loads(line)
        for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert docs[0]["question"] == "=1+1"
    assert docs[0]["answer"] == "[REDACTED] 产量 12"


def test_summary_preserves_skipped_incomplete_and_unreviewed_quality() -> None:
    summary = summarize(
        [
            Result("q1", "员工", "产量", 1, status="answer", elapsed_ms=100),
            Result("q2", "组长", "工资", 1, status="skipped"),
            Result("q3", "老板", "产量", 1, status="incomplete", elapsed_ms=400),
        ],
        planned=4,
    )
    assert summary["outcomes"] == {"answer": 1, "skipped": 1, "incomplete": 1}
    assert summary["recorded"] == 3
    assert summary["quality_review"] == "unreviewed"
    assert summary["client_latency_ms"] == {"p50": 100, "p95": 400}


@pytest.mark.asyncio
async def test_dry_run_is_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "questions.csv"
    path.write_text("role,question\n员工,我上个月的产量\n", encoding="utf-8")

    def refuse_client(*args: Any, **kwargs: Any) -> None:
        pytest.fail("dry-run must not construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", refuse_client)
    args = argparse.Namespace(
        csv=path, roles=None, limit=None, env_file=tmp_path / "missing.env", dry_run=True, repeat=2
    )
    assert await run_batch(args) == 0
    assert not (tmp_path / "results.csv").exists()


@pytest.mark.asyncio
async def test_batch_isolates_role_and_round_sessions_and_saves_only_trace_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "questions.csv"
    input_path.write_text(
        "case_id,role,question,session_group\n"
        "e1,员工,我上个月的产量,followup\n"
        "e2,员工,那本月呢,followup\n"
        "o1,老板,我上个月的产量,followup\n"
        "o2,老板,那本月呢,followup\n",
        encoding="utf-8",
    )
    values = {
        "MES_USER_CREDENTIAL_00": "synthetic-employee",
        "MES_USER_CREDENTIAL_99": "synthetic-owner",
    }

    def environment(_path: Path) -> dict[str, str]:
        return values

    monkeypatch.setattr("scripts.run_qa_batch.load_environment", environment)
    posts: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/live":
            return httpx.Response(200, json={"service": "factory-agent"})
        if request.method == "POST":
            posts.append((request.headers["X-Factory-Credential"], request.url.path))
            return httpx.Response(201, json={"interaction_id": f"it{len(posts)}"})
        if request.url.path.endswith("/trace"):
            return httpx.Response(
                200,
                json={
                    "ledger": {"total_ms": 100},
                    "processing_summary": {"cache_metrics": {"storage_hits": 1}},
                    "spans": [
                        {"kind": "llm", "payload": {"content": "CANARY-TRACE-SECRET"}},
                        {"kind": "mes"},
                    ],
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=result_frame() + complete_frame(),
        )

    original_client = httpx.AsyncClient

    def mock_client(**kwargs: Any) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    output = tmp_path / "run"
    args = argparse.Namespace(
        csv=input_path,
        roles=None,
        limit=None,
        env_file=tmp_path / ".env",
        dry_run=False,
        repeat=2,
        base_url="http://test.invalid",
        timeout=2,
        stream_retries=1,
        output_dir=output,
    )
    assert await run_batch(args) == 0
    assert len(posts) == 8
    assert posts[0] == posts[1]
    assert posts[2] == posts[3]
    assert posts[0][1] != posts[2][1]
    assert posts[0][1] != posts[4][1]
    assert posts[0][0] == "synthetic-employee"
    assert posts[2][0] == "synthetic-owner"
    encoded = (output / "results.jsonl").read_text(encoding="utf-8")
    assert "CANARY-TRACE-SECRET" not in encoded
    assert "synthetic-employee" not in encoded
    results = [json.loads(line) for line in encoded.splitlines()]
    assert results[0]["observed_llm_spans"] == 1
    assert results[0]["cache_metrics"] == {"storage_hits": 1}
    assert results[0]["ledger"] == {"total_ms": 100}
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["planned"] == summary["recorded"] == 8
    assert summary["by_role"]["员工"] == {"answer": 4}
    assert summary["by_role"]["老板"] == {"answer": 4}
