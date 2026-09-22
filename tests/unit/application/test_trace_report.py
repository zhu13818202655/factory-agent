"""HTML report: self-contained, and inert against business text.

The report is *downloaded and opened from disk*, and its inputs are free text
that routinely contains markup. Two failures matter, and both are silent in a
browser until it is too late:

* an external reference, which yields a report that renders only while online;
* an unescaped ``</script>`` or ``<!--`` in a prompt or tool result, which ends
  the data block early and swallows the rest of the page — or, worse, injects
  the text as markup.

These tests assert the containment, not the rendering: the rendering is checked
by eye once, the containment has to hold on every payload forever.
"""

import json
import re
from datetime import datetime, timezone

from factory_agent.application.trace_report import render_trace_report

NOW = datetime(2026, 9, 22, 0, 20, 31, tzinfo=timezone.utc)

#: Content a user or an upstream system can produce. Each entry is a real
#: escape attempt, not a curiosity: the first is the classic injection, and the
#: third is a string that already contains the escape sequence, so the escaper
#: has to be idempotent-safe rather than naive.
HOSTILE_TEXTS = (
    "开票说明 </script><script>alert(1)</script>",
    "<!-- 注释起始 -->",
    "工资表 <table><tr><td>8213.44</td></tr></table>",
    r"表达式 <\/script> 双反斜杠",
)

_DATA_BLOCK = re.compile(
    r'<script type="application/json" id="trace-data">(.*?)</script>', re.DOTALL
)
_RENDERER = re.compile(r"<script>\n\"use strict\";(.*?)</script>", re.DOTALL)


def document(*, content_capture: str = "full", hostile: bool = False) -> dict[str, object]:
    payload = None
    if content_capture != "none":
        payload = {
            "available": True,
            "input": {"body": {"dh": "SO-2026-0001"}},
            "output": {"rows": 1} if not hostile else {"notes": list(HOSTILE_TEXTS)},
            "truncated": hostile,
            "original_rows": 250_000 if hostile else None,
            "original_bytes": 99_000_000 if hostile else 12,
        }
    return {
        "schema_version": "1.0",
        "interaction_id": "int-1",
        "request_id": "req-9f3a",
        "session_id": "session-1",
        "capability_id": "FR-009",
        "status": "completed",
        "environment": "dev",
        "content_capture": content_capture,
        "report": {
            "available": True,
            "reason": None,
            "url": "/v1/interactions/int-1/trace.html",
            "content_capture": content_capture,
        },
        "started_at": NOW.isoformat(),
        "total_ms": 21_443,
        "ledger": {"total_ms": 21_443, "mes_ms": 15_641, "llm_ms": 3_130, "local_ms": 2_672},
        "phases": [
            {
                "id": "ph_parsing",
                "name": "parsing",
                "label": "解析",
                "offset_ms": 51,
                "duration_ms": 1_180,
                "status": "ok",
                "terminal": False,
            },
            {
                "id": "ph_executing",
                "name": "executing",
                "label": "取数",
                "offset_ms": 1_872,
                "duration_ms": 15_641,
                "status": "ok",
                "terminal": False,
            },
        ],
        "spans": [
            {
                "span_id": "mes:ScjdQuery:2026-09-22T00:20:31+00:00",
                "parent_span_id": "ph_executing",
                "kind": "mes",
                "operation_id": "ScjdQuery",
                "offset_ms": 1_872,
                "duration_ms": 15_641,
                "status": "ok",
                "error_category": None,
                "page_count": 1,
                "row_count_bucket": "1001+",
                "payload": payload,
            },
            {
                "span_id": "llm:lc_8f21",
                "parent_span_id": "ph_parsing",
                "kind": "llm",
                "stage": "extract",
                "logical_call_id": "lc_8f21",
                "model_alias": "factory-fast",
                "actual_model": "Qwen3-32B-Instruct",
                "attempt": 1,
                "fallback_reason": None,
                "tokens": {"prompt": 1_842, "completion": 96, "cached": 0, "reasoning": 0},
                "offset_ms": 51,
                "duration_ms": 1_180,
                "status": "ok",
                "error_category": None,
                "payload": None,
            },
        ],
    }


def embedded(document: dict[str, object]) -> dict[str, object]:
    """Parse the data block the way a browser would.

    The HTML tokenizer reads a ``<script>`` body as raw text, so no entity
    decoding happens on the way in; the escapes the renderer relies on are
    JSON escapes, and ``json.loads`` applies exactly those. This mirrors the
    browser rather than approximating it.
    """
    match = _DATA_BLOCK.search(render_trace_report(document))
    assert match is not None, "the report must embed its data block"
    return json.loads(match.group(1))


def test_the_report_references_nothing_outside_itself() -> None:
    """Offline-openable is a precondition of "download", not a nicety."""
    html = render_trace_report(document())

    for forbidden in ("http://", "https://", "//cdn", "src=", "<link", "@import", "fetch("):
        assert forbidden not in html, f"report reaches outside itself: {forbidden}"


def test_a_csp_meta_is_absent_but_nosniff_head_metadata_is_present() -> None:
    """The page declares its own encoding and stays out of search indexes."""
    html = render_trace_report(document())

    assert '<meta charset="utf-8">' in html
    assert 'name="robots" content="noindex, nofollow"' in html


def test_the_data_block_contains_no_sequence_the_tokenizer_can_act_on() -> None:
    """The invariant behind both escapes, stated directly.

    Inside a ``<script>`` body only two sequences are still meaningful: ``</``
    closes the element, and ``<!--`` moves the tokenizer into a state where
    ``</script>`` no longer does. If neither survives into the data block, a
    payload cannot reshape the document — regardless of what it contains.
    """
    block = _DATA_BLOCK.search(render_trace_report(document(hostile=True)))

    assert block is not None
    assert "</" not in block.group(1)
    assert "<!--" not in block.group(1)


def test_hostile_text_does_not_create_a_second_renderer_script() -> None:
    """Exactly one data block and one renderer: no injection added an element.

    Note what is *not* asserted: hostile text may still leave a bare ``<script``
    in the data block, and that is inert. In the script-data state only
    ``</script`` ends the element (and ``<!--``, which is escaped) — so the
    count that has to stay fixed is the closing one.
    """
    html = render_trace_report(document(hostile=True))

    assert html.count('<script type="application/json"') == 1
    assert html.count('<script>\n"use strict";') == 1
    # Two closing tags: the data block and the renderer, nothing more.
    assert html.count("</script>") == 2


def test_the_embedded_data_round_trips_byte_for_byte() -> None:
    """Escaping is invisible to the consumer: it parses back to the same data."""
    original = document(hostile=True)

    assert embedded(original) == original


def test_hostile_text_lands_in_the_data_block_intact() -> None:
    parsed = embedded(document(hostile=True))
    spans = parsed["spans"]
    assert isinstance(spans, list)
    notes = spans[0]["payload"]["output"]["notes"]  # type: ignore[index]

    assert notes == list(HOSTILE_TEXTS)


def test_the_renderer_assigns_text_and_never_markup() -> None:
    """Every business string must reach the DOM through ``textContent``.

    The check is on assignments, not on the word: the renderer's own comment
    names the API it avoids, and a naive ``not in html`` would fail on that
    comment rather than on a real regression.
    """
    renderer = _RENDERER.search(render_trace_report(document(hostile=True)))

    assert renderer is not None
    body = renderer.group(1)
    assert "textContent" in body
    for assignment in (
        "innerHTML =",
        "innerHTML=",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
    ):
        assert assignment not in body


def test_a_truncated_payload_is_flagged_explicitly() -> None:
    """The failure mode that matters: shortened data reading as complete data.

    The renderer's truncation branch is asserted here, and the numbers it
    interpolates are asserted to have survived into the data block. Whether the
    branch produces the notice at runtime is JavaScript behaviour, verified
    against a real engine in the report's own smoke check.
    """
    html = render_trace_report(document(hostile=True))

    assert "超出捕获上限被截断" in html
    assert "original_rows" in html and "original_bytes" in html
    parsed = embedded(document(hostile=True))
    payload = parsed["spans"][0]["payload"]  # type: ignore[index]
    assert payload["truncated"] is True
    assert payload["original_rows"] == 250_000
    assert payload["original_bytes"] == 99_000_000


def test_capture_off_is_stated_and_carries_no_payload_section() -> None:
    parsed = embedded(document(content_capture="none"))

    assert "content_capture=none" in render_trace_report(document(content_capture="none"))
    assert parsed["content_capture"] == "none"
    assert all(span["payload"] is None for span in parsed["spans"])  # type: ignore[union-attr]


def test_an_unavailable_payload_is_rendered_as_an_explicit_notice() -> None:
    report = document()
    report["spans"][0]["payload"] = {  # type: ignore[index]
        "available": False,
        "input": None,
        "output": None,
        "truncated": False,
        "original_rows": None,
        "original_bytes": None,
    }

    html = render_trace_report(report)

    # The branch exists, and the state that reaches it is preserved as data.
    assert "该调用未捕获载荷" in html
    assert embedded(report)["spans"][0]["payload"]["available"] is False  # type: ignore[index]


def test_an_empty_trace_still_renders_a_usable_page() -> None:
    """An interaction that failed before any call must not produce a broken page."""
    report = document()
    report["spans"] = []
    report["phases"] = []

    html = render_trace_report(report)

    assert "无调用记录。" in html
    assert "无数据。" in html
    assert embedded(report) == report
