"""Self-contained HTML trace report (§4.5, §4.10).

The report is *downloaded and opened from disk*, which decides the two design
constraints the plan calls hard requirements:

1. **Self-contained.** Every style, script and datum is inlined. No CDN, no
   ``fetch``, no relative asset. A report that needs the network is not a
   report — it is a page that happens to have been saved.
2. **Business text is embedded as data, never interpolated as markup.** Prompts,
   tool parameters and MES envelopes are free text that routinely contains
   ``<``, ``</script>`` or ``<!--``. Interpolating any of it into the document
   would let it escape its own element. So the whole document is injected once
   as JSON inside ``<script type="application/json">`` and the renderer builds
   every node with ``textContent``.

The escaping is the only subtle part, and it is worth stating plainly: inside a
``<script>`` element the HTML tokenizer reads raw text, and two sequences are
still meaningful.

* ``</`` can close the element early, so it becomes ``<\\/`` — a JSON-legal
  escape that parses back to ``/``.
* ``<!--`` moves the tokenizer into the *escaped* state, where ``</script>`` no
  longer closes the element and the remainder of the document is swallowed.
  Escaping the ``<`` as ``\\u003c`` defuses it while staying JSON-legal
  (``\\!`` would not be).

Both transformations are invisible to the consumer: the text is parsed back to
exactly what it was.
"""

import json
from collections.abc import Mapping
from typing import Any

_JSON_ESCAPES: tuple[tuple[str, str], ...] = (
    ("</", "<\\/"),
    ("<!--", "\\u003c!--"),
)


def render_trace_report(document: Mapping[str, Any]) -> str:
    """Render one assembled trace document as a standalone HTML page."""
    payload = _embed_json(document)
    return _TEMPLATE.replace("__TRACE_DATA__", payload)


def _embed_json(document: Mapping[str, Any]) -> str:
    text = json.dumps(document, ensure_ascii=False, default=str)
    for needle, replacement in _JSON_ESCAPES:
        text = text.replace(needle, replacement)
    return text


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>链路追踪报告</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #fff; --line: #e3e6ea; --text: #1f2328;
    --muted: #656d76; --accent: #1a56db; --ok: #1a7f37; --err: #b42318;
    --warn-bg: #fff8e5; --warn-line: #e8c76a; --warn-text: #7a5c00;
    --mes: #0e7490; --llm: #6b21a8; --local: #4b5563;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
      "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 19px; margin: 0 0 4px; font-weight: 650; }
  h2 { font-size: 15px; margin: 26px 0 10px; font-weight: 650; }
  .sub { color: var(--muted); font-size: 12.5px; margin-bottom: 18px; }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 16px 18px;
  }
  .cards { display: flex; flex-wrap: wrap; gap: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 16px; min-width: 150px; flex: 1 1 150px;
  }
  .card .k { color: var(--muted); font-size: 12px; }
  .card .v { font-size: 20px; font-weight: 650; margin-top: 2px; }
  .card .v small { font-size: 12px; font-weight: 400; color: var(--muted); }
  table { border-collapse: collapse; width: 100%; }
  th, td {
    text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--line);
    vertical-align: top; font-size: 13px;
  }
  th { color: var(--muted); font-weight: 600; font-size: 12px; white-space: nowrap; }
  code, pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
  }
  .pill {
    display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11.5px;
    border: 1px solid var(--line); background: #fafbfc; color: var(--muted);
  }
  .pill.ok { color: var(--ok); border-color: #b7dfc4; background: #f0f9f3; }
  .pill.error { color: var(--err); border-color: #f0c2bd; background: #fdf3f2; }
  .pill.mes { color: var(--mes); border-color: #b6dde6; background: #eff9fb; }
  .pill.llm { color: var(--llm); border-color: #ddd0f0; background: #f8f5fd; }
  .bars { display: flex; flex-direction: column; gap: 7px; }
  .bar-row { display: flex; align-items: center; gap: 10px; }
  .bar-label { width: 150px; flex: none; font-size: 12.5px; }
  .bar-track {
    position: relative; flex: 1; height: 18px; background: #eef1f4;
    border-radius: 4px; overflow: hidden;
  }
  .bar-fill { position: absolute; top: 0; bottom: 0; border-radius: 4px; }
  .bar-meta { width: 150px; flex: none; text-align: right; color: var(--muted); font-size: 12px; }
  .waterfall { position: relative; display: flex; flex-direction: column; gap: 2px; }
  .wf-row { display: flex; align-items: center; gap: 10px; }
  .wf-label {
    width: 210px; flex: none; font-size: 12px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .wf-track { position: relative; flex: 1; height: 16px; background: #f2f4f7; border-radius: 3px; }
  .wf-bar { position: absolute; top: 2px; height: 12px; border-radius: 3px; opacity: .92; }
  .wf-ms { width: 74px; flex: none; text-align: right; color: var(--muted); font-size: 11.5px; }
  .warn {
    background: var(--warn-bg); border: 1px solid var(--warn-line);
    color: var(--warn-text); border-radius: 8px; padding: 9px 12px; margin: 8px 0;
    font-size: 12.5px;
  }
  details { margin-top: 7px; }
  summary { cursor: pointer; font-size: 12.5px; color: var(--accent); }
  details pre {
    margin: 7px 0 0; padding: 10px; background: #f8f9fb; border: 1px solid var(--line);
    border-radius: 7px; max-height: 380px; overflow: auto; white-space: pre-wrap;
    word-break: break-word;
  }
  .empty { color: var(--muted); font-size: 13px; }
  /* —— JSON 树视图（载荷渲染）—— */
  .j-bar { display: flex; gap: 8px; align-items: center; margin: 6px 0 0; }
  .j-note { color: var(--muted); font-size: 11.5px; }
  .j-tree {
    margin: 6px 0 0; padding: 10px 12px; background: #f8f9fb; border: 1px solid var(--line);
    border-radius: 7px; max-height: 440px; overflow: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
    line-height: 1.55;
  }
  .j-rows { padding-left: 16px; border-left: 1px dotted var(--line); }
  .j-row { margin: 1px 0; }
  .j-key { color: #7c3aad; }
  .j-str { color: #1a7f37; word-break: break-all; }
  .j-num { color: #1a56db; }
  .j-bool { color: #b45309; }
  .j-null { color: #98a1ad; }
  .j-punc { color: #656d76; }
  .j-idx { color: #98a1ad; margin-right: 6px; }
  .j-count { color: var(--muted); font-size: 11px; }
  .j-badge {
    display: inline-block; background: #fdf3f2; color: var(--err);
    border: 1px solid #f0c2bd; border-radius: 999px; padding: 0 8px;
    font-size: 11px; margin-bottom: 4px; font-family: inherit;
  }
  .j-more, .j-dl {
    padding: 2px 10px; font: inherit; font-size: 11.5px; border: 1px solid var(--line);
    background: #fff; color: var(--accent); border-radius: 999px; cursor: pointer;
  }
  .j-more { display: block; margin: 4px 0; }
  .j-more:hover, .j-dl:hover { border-color: var(--accent); background: var(--bg); }
  footer { color: var(--muted); font-size: 11.5px; margin-top: 28px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>链路追踪报告</h1>
  <div class="sub" id="subtitle"></div>
  <div id="compliance"></div>
  <div class="cards" id="summary"></div>
  <h2>三段耗时</h2>
  <div class="panel bars" id="segments"></div>
  <h2>阶段汇总</h2>
  <div class="panel bars" id="phases"></div>
  <h2>调用瀑布图</h2>
  <div class="panel waterfall" id="waterfall"></div>
  <h2>调用明细</h2>
  <div class="panel" id="spans"></div>
  <footer id="footer"></footer>
</div>
<script type="application/json" id="trace-data">__TRACE_DATA__</script>
<script>
"use strict";
(function () {
  var raw = document.getElementById("trace-data").textContent || "{}";
  var trace = JSON.parse(raw);

  var KIND_COLOR = { mes: "#0e7490", llm: "#6b21a8", local: "#4b5563" };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    // Every piece of business text lands here. Assigning textContent and never
    // markup is what keeps a prompt that contains an HTML end tag inert.
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmt(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return ms + " ms";
    return (ms / 1000).toFixed(2) + " s";
  }

  function fmtBytes(n) {
    if (typeof n !== "number" || isNaN(n)) return "—";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  function pill(text, cls) {
    return el("span", "pill" + (cls ? " " + cls : ""), text);
  }

  function statusPill(status) {
    return pill(status, status === "ok" ? "ok" : status === "error" ? "error" : "");
  }

  document.getElementById("subtitle").textContent =
    "交互 " + trace.interaction_id + " · 会话 " + trace.session_id +
    " · 能力 " + (trace.capability_id || "—") + " · 开始 " + trace.started_at;

  // Compliance banner: the report's own provenance, stated before the data so a
  // reader knows whether content was captured at all.
  var compliance = document.getElementById("compliance");
  if (trace.content_capture === "none") {
    compliance.appendChild(el("div", "warn",
      "本报告不含内容捕获（content_capture=none）：只有阶段、耗时、状态与行数桶。"));
  } else {
    var limits = trace.capture_limits || {};
    var limitText = limits.max_payload_bytes
      ? "并受 " + fmtBytes(limits.max_payload_bytes) + " / " + limits.max_rows +
        " 行的每载荷上限约束（部署可配）"
      : "";
    compliance.appendChild(el("div", "warn",
      "内容捕获已开启（content_capture=" + trace.content_capture +
      "）。载荷已按凭据维度脱敏" + limitText + "；被截断的载荷在下方显式标注。"));
  }

  var cards = document.getElementById("summary");
  function card(key, value, suffix) {
    var box = el("div", "card");
    box.appendChild(el("div", "k", key));
    var v = el("div", "v", value);
    if (suffix) v.appendChild(el("small", null, " " + suffix));
    box.appendChild(v);
    return box;
  }
  cards.appendChild(card("总耗时", fmt(trace.total_ms)));
  cards.appendChild(card("状态", trace.status));
  cards.appendChild(card("阶段数", trace.phases.length));
  cards.appendChild(card("调用数", trace.spans.length));
  cards.appendChild(card("环境", trace.environment));
  cards.appendChild(card("schema", trace.schema_version));

  function bars(host, rows) {
    if (!rows.length) {
      host.appendChild(el("div", "empty", "无数据。"));
      return;
    }
    var max = 1;
    rows.forEach(function (r) { max = Math.max(max, r.value || 0); });
    rows.forEach(function (r) {
      var row = el("div", "bar-row");
      row.appendChild(el("div", "bar-label", r.label));
      var track = el("div", "bar-track");
      var fill = el("div", "bar-fill");
      fill.style.left = (100 * (r.offset || 0) / max).toFixed(3) + "%";
      fill.style.width = (100 * (r.value || 0) / max).toFixed(3) + "%";
      fill.style.background = r.color || "#1a56db";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el("div", "bar-meta", r.meta || fmt(r.value)));
      host.appendChild(row);
    });
  }

  var ledger = trace.ledger || {};
  bars(document.getElementById("segments"), [
    { label: "总耗时", value: ledger.total_ms || trace.total_ms, color: "#1f2328" },
    { label: "MES 取数", value: ledger.mes_ms || 0, color: KIND_COLOR.mes },
    { label: "大模型", value: ledger.llm_ms || 0, color: KIND_COLOR.llm },
    { label: "本地/其他", value: ledger.local_ms || 0, color: KIND_COLOR.local }
  ]);

  bars(document.getElementById("phases"), trace.phases.map(function (p) {
    return {
      label: (p.label || p.name) + (p.terminal ? "（收尾）" : ""),
      value: p.duration_ms, offset: p.offset_ms, color: "#1a56db",
      meta: fmt(p.duration_ms) + " @" + p.offset_ms + "ms"
    };
  }));

  var wf = document.getElementById("waterfall");
  if (!trace.spans.length) {
    wf.appendChild(el("div", "empty", "无调用记录。"));
  } else {
    var axis = Math.max(1, trace.total_ms || 1);
    trace.spans.forEach(function (s) {
      var row = el("div", "wf-row");
      var label = s.kind === "llm"
        ? "llm · " + s.stage + " · " + s.model_alias
        : "mes · " + s.operation_id + " · p" + s.page_count;
      row.appendChild(el("div", "wf-label", label));
      var track = el("div", "wf-track");
      var bar = el("div", "wf-bar");
      bar.style.left = (100 * s.offset_ms / axis).toFixed(3) + "%";
      bar.style.width = Math.max(0.6, 100 * s.duration_ms / axis).toFixed(3) + "%";
      bar.style.background = KIND_COLOR[s.kind] || KIND_COLOR.local;
      bar.title = s.span_id + " · " + s.status;
      track.appendChild(bar);
      row.appendChild(track);
      row.appendChild(el("div", "wf-ms", fmt(s.duration_ms)));
      wf.appendChild(row);
    });
  }

  /* ---- JSON 树视图 -----------------------------------------------------
   * 递归构建 DOM，叶子永远是 textContent —— 与整份报告同一条安全规则：
   * 业务文本绝不以 markup 形式插入。数组分页渲染（默认 100 项，按钮或滚动
   * 加载更多），结构-only 截断标记渲染成徽标 + 示例，每个载荷可下载完整 JSON。
   * 延迟到 details 展开才构建，报告打开时不会为所有 span 一次性建树。 */
  var JSON_PAGE = 100;
  var jsonObserver = ("IntersectionObserver" in window)
    ? new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) entry.target.click();
        });
      })
    : null;

  function jSpan(cls, text) {
    return el("span", "j-" + cls, text);
  }

  function jScalar(value) {
    if (value === null) return jSpan("null", "null");
    if (typeof value === "number") return jSpan("num", String(value));
    if (typeof value === "boolean") return jSpan("bool", String(value));
    return jSpan("str", JSON.stringify(value));
  }

  function isTruncMarker(value) {
    return !!value && typeof value === "object" && !Array.isArray(value)
      && "_truncated_items" in value && "_sample" in value;
  }

  function jsonArray(items) {
    var root = el("div", "j-obj");
    if (!items.length) {
      root.appendChild(jSpan("punc", "[]"));
      return root;
    }
    root.appendChild(jSpan("punc", "["));
    var body = el("div");
    root.appendChild(body);
    var shown = 0;
    var more = null;
    function renderBatch() {
      var end = Math.min(shown + JSON_PAGE, items.length);
      for (; shown < end; shown += 1) {
        var row = el("div", "j-row");
        row.appendChild(jSpan("idx", shown + ":"));
        row.appendChild(jsonValue(items[shown]));
        body.appendChild(row);
      }
      if (more && more.parentNode) more.parentNode.removeChild(more);
      if (shown < items.length) {
        more = el("button", "j-more",
          "显示更多（已渲染 " + shown + " / " + items.length + " 项）");
        more.type = "button";
        more.addEventListener("click", renderBatch);
        body.appendChild(more);
        if (jsonObserver) jsonObserver.observe(more);
      } else {
        body.appendChild(el("div", "j-count", "共 " + items.length + " 项"));
      }
    }
    renderBatch();
    root.appendChild(jSpan("punc", "]"));
    return root;
  }

  function truncatedMarkerView(marker) {
    var wrap = el("div");
    wrap.appendChild(el("span", "j-badge",
      "列表共 " + marker["_truncated_items"] + " 项：超出上限，仅保留结构示例"));
    var body = el("div");
    wrap.appendChild(body);
    var sample = Array.isArray(marker["_sample"]) ? marker["_sample"] : [marker["_sample"]];
    sample.forEach(function (item) {
      var row = el("div", "j-row");
      row.appendChild(jsonValue(item));
      body.appendChild(row);
    });
    return wrap;
  }

  function jsonObject(obj) {
    var root = el("div", "j-obj");
    var keys = Object.keys(obj);
    if (!keys.length) {
      root.appendChild(jSpan("punc", "{}"));
      return root;
    }
    root.appendChild(jSpan("punc", "{"));
    var body = el("div");
    root.appendChild(body);
    keys.forEach(function (key) {
      var row = el("div", "j-row");
      row.appendChild(jSpan("key", JSON.stringify(key)));
      row.appendChild(jSpan("punc", ": "));
      row.appendChild(jsonValue(obj[key]));
      body.appendChild(row);
    });
    root.appendChild(jSpan("punc", "}"));
    return root;
  }

  function jsonValue(value) {
    if (value === null || typeof value !== "object") return jScalar(value);
    if (Array.isArray(value)) return jsonArray(value);
    if (isTruncMarker(value)) return truncatedMarkerView(value);
    return jsonObject(value);
  }

  function downloadJson(filename, value) {
    var blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
  }

  function payloadBlock(span) {
    var p = span.payload;
    if (!p) return null;
    var box = el("div");
    if (!p.available) {
      box.appendChild(el("div", "empty", "该调用未捕获载荷（失败或早于开启时间）。"));
      return box;
    }
    if (p.truncated) {
      var limits = trace.capture_limits || {};
      box.appendChild(el("div", "warn",
        "⚠ 载荷超出捕获上限被截断，下方仅为结构与统计，不是完整数据。" +
        "截断前规模：" + (p.original_rows === null ? "—" : p.original_rows + " 行") +
        " · " + fmtBytes(p.original_bytes) +
        (limits.max_payload_bytes
          ? "（上限 " + fmtBytes(limits.max_payload_bytes) + " / " + limits.max_rows + " 行）"
          : "") +
        "。"));
    }
    var spanLabel = span.kind === "llm"
      ? span.stage + "-" + span.logical_call_id + "-" + span.attempt
      : span.operation_id + "-p" + span.page_count;
    var fileBase = "trace-" + trace.interaction_id + "-" +
      String(spanLabel).replace(/[^A-Za-z0-9_-]/g, "-");
    // 旧版捕获（envelope 序列化修复前入库）：载荷是一条 str(envelope) repr 字符串，
    // 结构已不可恢复 —— 明示出来，避免误以为是渲染问题。
    function isLegacyFlattened(value) {
      return !!(value && typeof value === "object" && typeof value.envelope === "string"
        && /^code=\d+ message=/.test(value.envelope));
    }
    [["输入", p.input, "input"], ["输出", p.output, "output"]].forEach(function (pair) {
      if (pair[1] === null || pair[1] === undefined) return;
      var d = document.createElement("details");
      d.appendChild(el("summary", null, pair[0]));
      var bar = el("div", "j-bar");
      bar.appendChild(el("span", "j-note", "JSON 树 · 分页渲染，可下载完整数据"));
      var dl = el("button", "j-dl", "下载 JSON");
      dl.type = "button";
      dl.addEventListener("click", function () {
        downloadJson(fileBase + "-" + pair[2] + ".json", pair[1]);
      });
      bar.appendChild(dl);
      d.appendChild(bar);
      if (isLegacyFlattened(pair[1])) {
        d.appendChild(el("div", "warn",
          "旧版捕获：该载荷在 envelope 序列化修复前入库，已被摊平为一条 repr 文本" +
          "（截断的旧载荷只保留了开头 4 KB，原始数据不可恢复）。重启后端后新发起的提问" +
          "即为完整结构化 JSON。"));
      }
      var tree = el("div", "j-tree");
      d.appendChild(tree);
      var built = false;
      d.addEventListener("toggle", function () {
        if (!d.open || built) return;
        built = true;
        tree.appendChild(jsonValue(pair[1]));
      });
      box.appendChild(d);
    });
    return box;
  }

  var spanHost = document.getElementById("spans");
  if (!trace.spans.length) {
    spanHost.appendChild(el("div", "empty", "无调用记录。"));
  } else {
    var table = document.createElement("table");
    var head = document.createElement("tr");
    ["类型", "标识", "偏移", "耗时", "状态", "补充"].forEach(function (t) {
      head.appendChild(el("th", null, t));
    });
    table.appendChild(head);
    trace.spans.forEach(function (s) {
      var tr = document.createElement("tr");
      var kindCell = document.createElement("td");
      kindCell.appendChild(pill(s.kind, s.kind === "llm" ? "llm" : "mes"));
      tr.appendChild(kindCell);

      var idCell = document.createElement("td");
      idCell.appendChild(el("code", null, s.kind === "llm"
        ? s.stage + " / " + s.logical_call_id + " #" + s.attempt
        : s.operation_id + " page=" + s.page_count));
      if (s.parent_span_id) {
        idCell.appendChild(el("div", "empty", "parent: " + s.parent_span_id));
      }
      tr.appendChild(idCell);

      tr.appendChild(el("td", null, s.offset_ms + " ms"));
      tr.appendChild(el("td", null, fmt(s.duration_ms)));

      var stCell = document.createElement("td");
      stCell.appendChild(statusPill(s.status));
      if (s.error_category) stCell.appendChild(el("div", "empty", s.error_category));
      tr.appendChild(stCell);

      var extra = document.createElement("td");
      if (s.kind === "llm") {
        extra.appendChild(el("div", null, "tokens " + s.tokens.prompt +
          " → " + s.tokens.completion + "（cache " + s.tokens.cached +
          " / reason " + s.tokens.reasoning + "）"));
        if (s.actual_model) extra.appendChild(el("div", "empty", s.actual_model));
        if (s.fallback_reason) {
          extra.appendChild(el("div", "empty", "fallback: " + s.fallback_reason));
        }
      } else {
        extra.appendChild(el("div", null, "行数桶 " + s.row_count_bucket));
      }
      var payload = payloadBlock(s);
      if (payload) extra.appendChild(payload);
      tr.appendChild(extra);

      table.appendChild(tr);
    });
    spanHost.appendChild(table);
  }

  var report = trace.report || {};
  document.getElementById("footer").textContent =
    "报告入口：" + (report.url || "—") +
    " · available=" + String(report.available) +
    " · reason=" + String(report.reason) +
    " · schema_version=" + trace.schema_version +
    " · 本文件自包含，可离线打开。";
})();
</script>
</body>
</html>
"""


__all__ = ["render_trace_report"]
