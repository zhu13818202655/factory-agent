import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, ValidationError

ROOT = Path(__file__).resolve().parents[1]
ROLE_CODES = {"员工": "00", "组长": "01", "管理": "02", "老板": "99"}
ROLE_ALIASES = {
    **{name: name for name in ROLE_CODES},
    **{code: name for name, code in ROLE_CODES.items()},
    "employee": "员工",
    "group_leader": "组长",
    "manager": "管理",
    "owner": "老板",
}
OUTCOMES = frozenset(
    {
        "answer",
        "empty",
        "incomplete",
        "clarification",
        "denied",
        "rejected",
        "chat",
        "failed",
        "cancelled",
    }
)
TERMINALS = {"interaction.completed", "interaction.failed", "interaction.cancelled"}
MAX_EVENT_CHARS = 16 * 1024 * 1024


class Case(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    role: Literal["员工", "组长", "管理", "老板"]
    question: str = Field(min_length=1, max_length=2000)
    expected_capability: str = ""
    expected_outcome: str = ""
    session_group: str = ""
    category: str = ""


class CreatedInteraction(BaseModel):
    interaction_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class Event(BaseModel):
    name: str
    sequence: int = Field(ge=0)
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Options:
    base_url: str
    timeout_seconds: float = 360
    stream_retries: int = 1
    credential_header: str = "X-Factory-Credential"


@dataclass
class Result:
    case_id: str
    role: str
    question: str
    round: int
    category: str = ""
    session_group: str = ""
    session_id: str = ""
    interaction_id: str = ""
    status: str = "not_run"
    error_category: str = ""
    answer: str = ""
    clarification: str = ""
    capability_id: str = ""
    expected_capability: str = ""
    expected_outcome: str = ""
    expectation_match: bool | None = None
    elapsed_ms: int | None = None
    first_event_ms: int | None = None
    row_count: int | None = None
    incomplete: bool | None = None
    incomplete_reason: str | None = None
    cache_source: dict[str, Any] | None = None
    time_range: dict[str, Any] | None = None
    card: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list[str])
    trace_status: str = "not_requested"
    ledger: dict[str, Any] | None = None
    cache_metrics: dict[str, Any] | None = None
    observed_llm_spans: int | None = None
    observed_mes_spans: int | None = None


class ProtocolError(ValueError):
    pass


def load_cases(path: Path) -> list[Case]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"role", "question"} <= set(reader.fieldnames or ()):
            raise ValueError("CSV requires role and question columns")
        cases: list[Case] = []
        seen: set[str] = set()
        for number, row in enumerate(reader, start=2):
            if not any(value for value in row.values()):
                continue
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV row {number} has an invalid number of fields")
            role = ROLE_ALIASES.get((row.get("role") or "").strip())
            if role is None:
                raise ValueError(f"CSV row {number} has an unknown role")
            fields = {key: (row.get(key) or "").strip() for key in Case.model_fields}
            fields["role"] = role
            fields["case_id"] = fields["case_id"] or f"q{number - 1:04d}"
            try:
                case = Case.model_validate(fields)
            except ValidationError:
                raise ValueError(f"CSV row {number} has invalid case fields") from None
            expected: set[str] = (
                set(case.expected_outcome.split("|")) if case.expected_outcome else set()
            )
            if not expected <= OUTCOMES:
                raise ValueError(f"CSV row {number} has an unknown expected outcome")
            if case.case_id in seen:
                raise ValueError(f"CSV row {number} duplicates a case_id")
            seen.add(case.case_id)
            cases.append(case)
    if not cases:
        raise ValueError("CSV contains no cases")
    return cases


def load_environment(path: Path) -> dict[str, str]:
    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    return {**values, **os.environ}


def role_credentials(values: Mapping[str, str]) -> dict[str, SecretStr]:
    credentials: dict[str, SecretStr] = {}
    for role, code in ROLE_CODES.items():
        for key in (f"MES_USER_CREDENTIAL_{code}", f"FACTORY_AGENT_MES_USER_CREDENTIAL_{code}"):
            raw = values.get(key, "").strip()
            if not raw:
                continue
            if raw.startswith("["):
                try:
                    entries: object = json.loads(raw)
                except ValueError:
                    break
                if not isinstance(entries, list):
                    break
                items = cast(list[object], entries)
                if len(items) != 1:
                    break
                first = items[0]
                if not isinstance(first, str) or not first.strip():
                    break
                raw = first.strip()
            credentials[role] = SecretStr(raw)
            break
    return credentials


class SecretFilter:
    def __init__(self, values: Mapping[str, str]) -> None:
        secrets: set[str] = set()
        for key, value in values.items():
            if not value:
                continue
            if re.search(r"CREDENTIAL|API_KEY|SECRET|API_TOKEN|KEY_DEEPSEEK|PASSWORD", key):
                secrets.add(value)
                if value.startswith("["):
                    try:
                        entries: object = json.loads(value)
                    except ValueError:
                        entries = None
                    if isinstance(entries, list):
                        secrets.update(
                            item for item in cast(list[object], entries) if isinstance(item, str)
                        )
            if key.endswith("_URL"):
                try:
                    password = urlsplit(value).password
                except ValueError:
                    password = None
                if password:
                    secrets.add(password)
                    secrets.add(unquote(password))
        self._secrets = tuple(sorted((value for value in secrets if value), key=len, reverse=True))

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"\b(?:postgres(?:ql)?|redis)://\S+", "[REDACTED]", value)
            return re.sub(r"\bBearer\s+\S+", "Bearer [REDACTED]", value, flags=re.IGNORECASE)
        if isinstance(value, dict):
            items = cast(dict[str, Any], value)
            return {
                key: "[REDACTED]"
                if re.fullmatch(
                    r"app_key|accessToken|access_token|sign|password|authorization|credential|secret",
                    key,
                    flags=re.IGNORECASE,
                )
                else self.clean(item)
                for key, item in items.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in cast(Sequence[Any], value)]
        return value


async def parse_events(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    name, sequence = "message", "0"
    data_lines: list[str] = []
    size = 0
    async for line in lines:
        if not line:
            if data_lines:
                try:
                    yield Event.model_validate(
                        {
                            "name": name,
                            "sequence": int(sequence),
                            "data": json.loads("\n".join(data_lines)),
                        }
                    )
                except (ValueError, ValidationError):
                    raise ProtocolError("invalid_event") from None
            name, sequence, data_lines, size = "message", "0", [], 0
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value.removeprefix(" ")
        size += len(value)
        if size > MAX_EVENT_CHARS:
            raise ProtocolError("event_too_large")
        if key == "event":
            name = value
        elif key == "id":
            sequence = value
        elif key == "data":
            data_lines.append(value)


def apply_event(result: Result, event: Event) -> None:
    result.events.append(event.name)
    payload = event.data
    if event.name == "interaction.result":
        result.answer = str(payload.get("answer") or "")
        result.capability_id = str(payload.get("capability_id") or "")
        result.row_count = payload.get("row_count")
        result.incomplete = payload.get("incomplete")
        result.incomplete_reason = payload.get("incomplete_reason")
        result.cache_source = payload.get("cache_source")
        result.time_range = payload.get("time_range")
        result.card = payload.get("card")
        result.status = (
            "incomplete" if result.incomplete else ("empty" if result.row_count == 0 else "answer")
        )
    elif event.name == "interaction.answer":
        result.answer = str(payload.get("text") or "")
        result.status = "chat"
    elif event.name == "interaction.clarification":
        result.clarification = str(payload.get("question") or "")
        result.status = "clarification"
    elif event.name in {"interaction.failed", "interaction.cancelled"}:
        result.error_category = str(payload.get("error_category") or "unknown")
        result.answer = str(payload.get("message") or "")
        if event.name == "interaction.cancelled":
            result.status = "cancelled"
        elif "forbidden" in result.error_category:
            result.status = "denied"
        elif result.error_category == "time_range_exceeds_limit":
            result.status = "rejected"
        else:
            result.status = "failed"
    elif event.name == "interaction.completed" and result.status == "not_run":
        result.status = "failed"
        result.error_category = "missing_answer_event"


def assess(result: Result) -> None:
    checks: list[bool] = []
    if result.status in {"not_run", "skipped"}:
        return
    if result.expected_outcome:
        checks.append(result.status in result.expected_outcome.split("|"))
    if result.expected_capability and result.capability_id:
        actual = result.capability_id.split("-", 2)
        base = "-".join(actual[:2]) if len(actual) >= 2 else result.capability_id
        checks.append(base in result.expected_capability.split("|"))
    result.expectation_match = all(checks) if checks else None


async def cancel_run(
    client: httpx.AsyncClient, interaction_id: str, headers: dict[str, str]
) -> None:
    try:
        await client.post(f"/v1/interactions/{interaction_id}/cancel", headers=headers, timeout=5)
    except httpx.HTTPError:
        pass


async def read_trace(client: httpx.AsyncClient, result: Result, headers: dict[str, str]) -> None:
    try:
        response = await client.get(
            f"/v1/interactions/{result.interaction_id}/trace", headers=headers, timeout=10
        )
        if response.status_code != 200:
            result.trace_status = f"http_{response.status_code}"
            return
        doc: dict[str, Any] = response.json()
        result.ledger = doc.get("ledger")
        summary: dict[str, Any] = doc.get("processing_summary") or {}
        result.cache_metrics = summary.get("cache_metrics")
        spans: list[dict[str, Any]] = doc.get("spans") or []
        result.observed_llm_spans = sum(span.get("kind") == "llm" for span in spans)
        result.observed_mes_spans = sum(span.get("kind") == "mes" for span in spans)
        result.trace_status = "available"
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        result.trace_status = "unavailable"


async def run_case(
    client: httpx.AsyncClient,
    case: Case,
    credential: SecretStr | None,
    options: Options,
    *,
    round_number: int,
    session_id: str,
) -> Result:
    result = Result(
        case_id=case.case_id,
        role=case.role,
        question=case.question,
        round=round_number,
        category=case.category,
        session_group=case.session_group,
        session_id=session_id,
        expected_capability=case.expected_capability,
        expected_outcome=case.expected_outcome,
    )
    if credential is None:
        result.status, result.error_category = "skipped", "credential_not_configured"
        return result
    headers = {options.credential_header: credential.get_secret_value()}
    started = perf_counter()
    terminal_received = False
    try:
        async with asyncio.timeout(options.timeout_seconds):
            created = await client.post(
                f"/v1/sessions/{session_id}/interactions",
                json={"text": case.question},
                headers=headers,
            )
            if created.status_code != 201:
                result.status = "failed"
                result.error_category = f"start_http_{created.status_code}"
                return result
            result.interaction_id = CreatedInteraction.model_validate(created.json()).interaction_id
            sequence = 0
            for attempt in range(options.stream_retries + 1):
                try:
                    async with client.stream(
                        "GET",
                        f"/v1/interactions/{result.interaction_id}/stream",
                        headers={**headers, "Last-Event-ID": str(sequence)},
                    ) as response:
                        response.raise_for_status()
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            raise ProtocolError("invalid_content_type")
                        async for event in parse_events(response.aiter_lines()):
                            if event.sequence <= sequence and event.name != "interaction.heartbeat":
                                continue
                            if result.first_event_ms is None:
                                result.first_event_ms = round((perf_counter() - started) * 1000)
                            sequence = max(sequence, event.sequence)
                            apply_event(result, event)
                            if event.name in TERMINALS:
                                terminal_received = True
                                break
                    if terminal_received:
                        break
                except httpx.TransportError:
                    if attempt == options.stream_retries:
                        raise
            if not terminal_received:
                raise ProtocolError("stream_ended_without_terminal")
    except TimeoutError:
        result.status, result.error_category = "failed", "client_timeout"
    except ProtocolError as error:
        result.status, result.error_category = "failed", str(error)
    except httpx.HTTPStatusError as error:
        result.status, result.error_category = "failed", f"stream_http_{error.response.status_code}"
    except httpx.HTTPError:
        result.status, result.error_category = "failed", "transport_error"
    except (ValueError, ValidationError):
        result.status, result.error_category = "failed", "invalid_response"
    finally:
        result.elapsed_ms = round((perf_counter() - started) * 1000)
        if result.interaction_id and not terminal_received:
            await cancel_run(client, result.interaction_id, headers)
        assess(result)
    if result.interaction_id and terminal_received:
        await read_trace(client, result, headers)
    return result


def csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_result(output: Path, result: Result, secret_filter: SecretFilter) -> None:
    document: dict[str, Any] = secret_filter.clean(asdict(result))
    with (output / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, allow_nan=False) + "\n")
    csv_path = output / "results.csv"
    fields = [
        name for name in document if name not in {"card", "cache_metrics", "ledger", "events"}
    ]
    row = {
        key: csv_cell(
            json.dumps(document[key], ensure_ascii=False)
            if isinstance(document[key], dict)
            else document[key]
        )
        for key in fields
    }
    exists = csv_path.exists()
    with csv_path.open("a", encoding="utf-8" if exists else "utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def summarize(results: Sequence[Result], planned: int) -> dict[str, Any]:
    durations = sorted(item.elapsed_ms for item in results if item.elapsed_ms is not None)

    def percentile(fraction: float) -> int | None:
        return durations[max(0, math.ceil(len(durations) * fraction) - 1)] if durations else None

    return {
        "planned": planned,
        "recorded": len(results),
        "outcomes": dict(Counter(item.status for item in results)),
        "by_role": {
            role: dict(Counter(item.status for item in results if item.role == role))
            for role in ROLE_CODES
        },
        "expectation_matched": sum(item.expectation_match is True for item in results),
        "expectation_mismatched": sum(item.expectation_match is False for item in results),
        "expectation_unassessed": sum(item.expectation_match is None for item in results),
        "client_latency_ms": {"p50": percentile(0.5), "p95": percentile(0.95)},
        "latency_scope": "所有已发出请求的用例，包括拒绝和执行失败；不含 trace 读取。",
        "quality_review": "unreviewed",
        "notes": [
            "完成、预期状态匹配均不代表业务数值正确；需核对回答和数据口径。",
            "重复轮次不保证冷缓存或热缓存；以 cache_source 和缓存指标为依据。",
            "trace span 数为观测到的记录数；缺失计量不能当作零次实际调用。",
        ],
    }


async def run_batch(args: argparse.Namespace) -> int:
    cases = load_cases(args.csv)
    if args.roles:
        selected = {ROLE_ALIASES[item] for item in args.roles}
        cases = [case for case in cases if case.role in selected]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("No cases match the selected roles")
    values = load_environment(args.env_file)
    credentials = role_credentials(values)
    secret_filter = SecretFilter(values)
    counts = Counter(case.role for case in cases)
    for role, count in counts.items():
        state = "已配置" if role in credentials else "缺少凭据"
        print(f"{role}: {count} 个问题；{state}")
    if args.dry_run:
        print(f"CSV 校验通过；计划 {len(cases) * args.repeat} 次问答；未发送任何请求。")
        return 0
    if any(secret_filter.clean(case.question) != case.question for case in cases):
        raise ValueError("A question contains a configured secret; no requests sent")
    if any("REMOTE_SERVER_IP" in value for key, value in values.items() if key.endswith("_URL")):
        raise ValueError("Replace REMOTE_SERVER_IP in .env before running")
    base_url = (
        args.base_url
        or values.get("FACTORY_AGENT_QA_BASE_URL")
        or f"http://127.0.0.1:{values.get('FACTORY_AGENT_PORT', '8000')}"
    )
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base-url must be an HTTP origin without credentials or a path")
    options = Options(
        base_url,
        args.timeout,
        args.stream_retries,
        values.get("FACTORY_AGENT_CREDENTIAL_HEADER", "X-Factory-Credential"),
    )
    run_id = uuid4().hex
    started_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    output = args.output_dir or ROOT / "data" / "qa-runs" / (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-" + run_id[:8]
    )
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("manifest.json", "results.csv", "results.jsonl")):
        raise ValueError("Output directory already contains a run; choose an empty directory")
    manifest = {
        "run_id": run_id,
        "started_at": started_at,
        "input_sha256": hashlib.sha256(args.csv.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "base_url": base_url,
        "repeat": args.repeat,
        "planned": len(cases) * args.repeat,
        "cases": [case.model_dump() for case in cases],
        "roles_configured": [role for role in ROLE_CODES if role in credentials],
        "cache_mode_requested": values.get("FACTORY_AGENT_RESULT_CACHE_MODE", "readwrite"),
    }
    (output / "manifest.json").write_text(
        json.dumps(secret_filter.clean(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    results: list[Result] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(30, read=None),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        try:
            health = await client.get("/health/live", timeout=5)
            if health.status_code != 200 or health.json().get("service") != "factory-agent":
                raise ValueError("Endpoint is not a live factory-agent API")
        except httpx.HTTPError:
            raise ValueError("Cannot reach factory-agent API; start it before running") from None
        try:
            for round_number in range(1, args.repeat + 1):
                sessions: dict[tuple[str, str], str] = {}
                for case in cases:
                    key = (case.role, case.session_group or case.case_id)
                    session_id = sessions.setdefault(key, uuid4().hex)
                    result = await run_case(
                        client,
                        case,
                        credentials.get(case.role),
                        options,
                        round_number=round_number,
                        session_id=session_id,
                    )
                    write_result(output, result, secret_filter)
                    results.append(result)
                    progress = (
                        f"[{len(results)}/{len(cases) * args.repeat}] {case.case_id} {case.role} "
                        f"round={round_number} {result.status} {result.elapsed_ms}ms"
                    )
                    print(secret_filter.clean(progress))
        finally:
            summary = summarize(results, len(cases) * args.repeat)
            (output / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print(f"结果目录: {output}")
    return (
        1
        if any(
            item.status in {"failed", "skipped", "cancelled"} or item.expectation_match is False
            for item in results
        )
        else 0
    )


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            cast("TextIOWrapper[Any]", stream).reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        description="Read role/question CSV, call factory-agent HTTP/SSE, save auditable results."
    )
    parser.add_argument("--csv", type=Path, default=ROOT / "tests" / "data" / "qa_questions.csv")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--base-url")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--roles", nargs="+", choices=sorted(ROLE_ALIASES))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=360)
    parser.add_argument("--stream-retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if (
        args.repeat < 1
        or args.timeout <= 0
        or not math.isfinite(args.timeout)
        or args.stream_retries < 0
        or (args.limit is not None and args.limit < 1)
    ):
        parser.error("repeat/timeout/limit must be positive; stream-retries must be nonnegative")
    try:
        return asyncio.run(run_batch(args))
    except ValueError as error:
        safe_message = SecretFilter(load_environment(args.env_file)).clean(str(error))
        print(f"运行未开始或已中止：{safe_message}", file=sys.stderr)
        return 2
    except OSError:
        print("无法读取配置或写入结果；检查文件路径和目录权限。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中止；已完成的问题结果保留在输出目录。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
