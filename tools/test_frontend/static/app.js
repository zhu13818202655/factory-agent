/* Factory Agent 四角色测试台 —— 前端逻辑
 *
 * 所有 /v1 请求都发给同源代理（tools/test_frontend/server.py），代理按
 * X-Dev-Role 头选择角色并注入 X-Factory-Credential。SSE 用 fetch 流读取
 * （EventSource 无法携带自定义头），断线按文档约定用 Last-Event-ID 续传。
 */
"use strict";

const ROLE_HEADER = "X-Dev-Role";
const SID_KEY = (role) => `fa_sid_${role}`;
const ROLE_COLORS = { "00": "#2aa198", "01": "#4c9aff", "02": "#9a7bd8", "99": "#e0a03c" };
const STATE_LABEL = {
  parsing: "解析中",
  clarifying: "追问",
  authorizing: "鉴权",
  executing: "取数中",
  composing: "计算中",
  answered: "完成",
  cancelled: "已取消",
  failed: "失败",
  archived: "归档",
};
const STAGE_ORDER = ["接收", "解析", "追问", "鉴权", "取数", "计算", "完成"];

const state = {
  role: null,
  sid: null,
  interactionId: null,
  lastEventId: 0,
  terminal: false,
  busy: false,
  attempts: 0,
  frames: [],      // 本轮原始事件（用于“查看原始事件”）
  trail: [],       // 阶段轨迹
  controller: null,
};

/* ---------------- DOM ---------------- */
const $ = (id) => document.getElementById(id);
const viewSelect = $("view-select");
const viewChat = $("view-chat");
const roleGrid = $("role-grid");
const messagesEl = $("messages");
const healthPill = $("health-pill");
const inputEl = $("input");
const quickWrap = $("quick-questions");
const toastEl = $("toast");
const connState = $("conn-state");

/* ---------------- toast ---------------- */
let toastTimer = null;
function toast(text, ms = 3600) {
  toastEl.textContent = text;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, ms);
}

/* ---------------- HTTP helper ---------------- */
async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  headers[ROLE_HEADER] = state.role || "99";
  const resp = await fetch(path, Object.assign({}, options, { headers }));
  return resp;
}

/* ---------------- 角色选择 ---------------- */
async function loadRoles() {
  try {
    const resp = await fetch("/api/roles");
    const roles = await resp.json();
    renderRoles(roles);
  } catch (err) {
    roleGrid.innerHTML = `<div class="empty-state">无法加载角色列表：${err.message}</div>`;
  }
}

function renderRoles(roles) {
  roleGrid.innerHTML = "";
  for (const r of roles) {
    const color = ROLE_COLORS[r.code] || "#4c9aff";
    const card = document.createElement("div");
    card.className = "role-card";
    card.style.setProperty("--rc", color);
    card.innerHTML = `
      <div class="code">${r.code}</div>
      <div class="name-row"><span class="dot"></span><h3>${r.name}</h3></div>
      <div class="scope">${r.scope}</div>
      <div class="actions">
        ${r.configured
          ? `<button class="enter-btn">进入对话</button>`
          : `<span class="unset">未配置凭据<br/><small>服务端 .env 缺少 MES_USER_CREDENTIAL_${r.code}</small></span>`}
      </div>`;
    card.querySelector(".enter-btn")?.addEventListener("click", () => enterRole(r.code, r.name));
    roleGrid.appendChild(card);
  }
}

/* ---------------- 进入角色 / 恢复会话 ---------------- */
function enterRole(code, name) {
  state.role = code;
  state.sid = localStorage.getItem(SID_KEY(code));
  if (!state.sid) {
    state.sid = `sess_${code}_${Date.now().toString(36)}`;
    localStorage.setItem(SID_KEY(code), state.sid);
  }
  $("role-badge").textContent = code;
  $("role-badge").style.setProperty("--rc", ROLE_COLORS[code] || "#4c9aff");
  $("role-name").textContent = `${name}（${code}）`;
  $("session-chip").textContent = state.sid;
  $("session-chip").title = `会话 ID（已存浏览器，刷新可恢复）：${state.sid}`;
  viewSelect.hidden = true;
  viewChat.hidden = false;
  messagesEl.innerHTML = "";
  connState.textContent = "连接中…";
  connState.classList.remove("live");
  loadQuickQuestions();
  restoreMessages().then(() => { connState.textContent = "已连接"; connState.classList.add("live"); });
}

function exitToSelect() {
  if (state.busy) stopInteraction();
  state.role = null;
  viewChat.hidden = true;
  viewSelect.hidden = false;
  messagesEl.innerHTML = "";
  quickWrap.classList.remove("hidden");
  loadRoles();
}

/* ---------------- 快捷问题 ---------------- */
async function loadQuickQuestions() {
  try {
    const resp = await api("/v1/quick-questions");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const items = await resp.json();
    quickWrap.classList.remove("hidden");
    quickWrap.innerHTML = "";
    for (const q of items) {
      const b = document.createElement("button");
      b.textContent = q.text;
      b.title = `${q.capability_id}`;
      b.addEventListener("click", () => send(q.text));
      quickWrap.appendChild(b);
    }
  } catch {
    quickWrap.classList.add("hidden");
  }
}

/* ---------------- 历史消息恢复 ---------------- */
async function restoreMessages() {
  try {
    const resp = await api(`/v1/sessions/${encodeURIComponent(state.sid)}/messages?limit=200`);
    if (!resp.ok) return;
    const page = await resp.json();
    messagesEl.innerHTML = "";
    if (!page.items.length) showEmptyHint();
    for (const m of page.items) renderRestoredMessage(m);
    scrollBottom();
  } catch {
    showEmptyHint();
  }
}

function showEmptyHint() {
  const div = document.createElement("div");
  div.className = "empty-state";
  div.textContent = "还没有对话记录。用下面的快捷问题或直接输入开始测试。";
  messagesEl.appendChild(div);
}

function renderRestoredMessage(m) {
  const kind = m.kind || "plain_text";
  const role = m.role === "user" ? "user" : kind === "error" ? "error" : kind === "phase" || role_system(m) ? "system" : "assistant";
  const node = document.createElement("div");
  node.className = `msg ${role}`;
  if (kind === "result_table" && renderRestoredResultCard(m, node)) {
    // full result card rebuilt from message payload
  } else {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (kind === "phase") {
      bubble.textContent = `⚙ ${m.text}`;
    } else if (kind === "clarification") {
      bubble.innerHTML = `<span class="kind-tag">追问</span>${escapeHtml(m.text)}`;
    } else if (kind === "error") {
      bubble.innerHTML = `<span class="kind-tag">${role_system(m) ? "系统" : "错误"}</span>${escapeHtml(m.text)}`;
    } else {
      bubble.textContent = m.text;
    }
    node.appendChild(bubble);
  }
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `#${m.sequence} ${m.kind}`;
  node.appendChild(meta);
  messagesEl.appendChild(node);
  return node;
}

/* 历史消息中的 result_table → 重建结果卡片（列/行数/完整性/导出按钮）。
 * 需要消息 payload 携带列元数据；旧消息无 payload 时上层回退为单句摘要。 */
function renderRestoredResultCard(m, node) {
  const p = m.payload;
  if (!p || !Array.isArray(p.columns) || !p.columns.length) return false;
  const cols = p.columns;
  const rowCount = p.row_count ?? "?";
  const incomplete = p.incomplete === true;
  const warnCols = cols.filter((c) => /delivery_warning|days_remaining/.test(c));
  node.classList.add("result-card");

  const card = document.createElement("div");
  card.className = "phase-card";
  const line = document.createElement("div");
  line.className = "line";
  const st = document.createElement("span");
  st.className = "phase-text";
  st.textContent = "结果就绪";
  line.appendChild(st);
  card.appendChild(line);

  const head = document.createElement("div");
  head.className = "head";
  head.innerHTML = `
    <span class="title">结果卡片</span>
    <span class="chip">${escapeHtml(p.capability_id || "")}</span>
    <span class="tag ${incomplete ? "warn" : "ok"}">${incomplete ? "不完整" : "完整"}</span>
    ${incomplete && p.incomplete_reason ? `<span class="tag warn">${escapeHtml(reasonLabel(p.incomplete_reason))}</span>` : ""}
    <span class="tag">${rowCount} 行</span>`;
  card.appendChild(head);

  const colRow = document.createElement("div");
  colRow.className = "cols";
  for (const c of cols) {
    const s = document.createElement("span");
    s.textContent = c;
    if (warnCols.includes(c)) { s.classList.add("warn-col"); s.title = "异常数据高亮列（后端只下发标记，前端渲染）"; }
    colRow.appendChild(s);
  }
  card.appendChild(colRow);

  if (p.artifact_id) {
    const btn = document.createElement("button");
    btn.className = "export-btn";
    btn.textContent = "导出报表（xlsx）";
    btn.addEventListener("click", () => downloadArtifact(p.artifact_id, p.capability_id || "result", btn));
    card.appendChild(btn);
  }
  node.appendChild(card);
  return true;
}

function role_system(m) { return m.role === "system"; }

/* ---------------- 发送 / 流式消费 ---------------- */
function send(textRaw) {
  const text = (textRaw ?? inputEl.value).trim();
  if (!text || state.busy || !state.role) return;
  inputEl.value = "";
  autosize();
  appendUserMessage(text);
  startInteraction(text);
}

function appendUserMessage(text) {
  const node = document.createElement("div");
  node.className = "msg user";
  node.innerHTML = `<div class="bubble"></div>`;
  node.querySelector(".bubble").textContent = text;
  messagesEl.appendChild(node);
  scrollBottom();
}

async function startInteraction(text) {
  setBusy(true);
  state.frames = [];
  state.trail = [];
  state.lastEventId = 0;
  state.terminal = false;
  state.attempts = 0;
  try {
    const resp = await api(`/v1/sessions/${encodeURIComponent(state.sid)}/interactions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (resp.status === 201) {
      const view = await resp.json();
      state.interactionId = view.interaction_id;
      await consumeStream(view.interaction_id);
    } else {
      const err = await readError(resp);
      toast(`发起失败 HTTP ${resp.status}：${err}`);
    }
  } catch (err) {
    toast(`请求异常：${err.message}`);
  } finally {
    setBusy(false);
    await restoreMessages();
  }
}

async function readError(resp) {
  try {
    const body = await resp.json();
    if (body.detail && typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return JSON.stringify(body.detail[0]);
    return JSON.stringify(body);
  } catch {
    return resp.statusText;
  }
}

/* 消费 SSE：fetch 流读取，按 id:/event:/data: 解析 */
async function consumeStream(interactionId) {
  let buffer = "";
  const decoder = new TextDecoder();

  while (!state.terminal) {
    if (state.attempts >= 15) { toast("SSE 重连次数过多，已放弃（请检查后端日志）"); break; }
    const ac = new AbortController();
    state.controller = ac;
    const headers = {};
    if (state.lastEventId > 0) headers["Last-Event-ID"] = String(state.lastEventId);
    headers[ROLE_HEADER] = state.role;
    if (state.attempts > 0) connState.textContent = `重连中(${state.attempts})…`;

    try {
      const resp = await fetch(`/v1/interactions/${interactionId}/stream`, { headers, signal: ac.signal });
      if (!resp.ok) { toast(`SSE HTTP ${resp.status}`); break; }
      const reader = resp.body.getReader();
      for (;;) {
        const { value, done } = await raceTimeout(reader.read(), 45_000, ac);
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) handleFrame(frame.trim());
        if (state.terminal) break;
      }
      if (!state.terminal) {
        // 连接被服务端关闭但未见终态 → 断点续传重连
        state.attempts += 1;
        await sleep(800 * state.attempts);
      }
    } catch (err) {
      if (state.terminal) break;
      if (err.name === "AbortError") { toast("读取超时，正在断点续传…", 1800); }
      state.attempts += 1;
      await sleep(600 * Math.min(state.attempts, 6));
    } finally {
      ac.abort();
    }
  }
}

function raceTimeout(promise, ms, ac) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { ac.abort(); }, ms);
    promise.then(
      (v) => { clearTimeout(t); resolve(v); },
      (e) => { clearTimeout(t); reject(e); }
    );
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function handleFrame(frame) {
  let id = 0, event = "", dataText = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) id = parseInt(line.slice(3).trim(), 10) || 0;
    else if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataText += line.slice(5).trim();
  }
  if (!event && !dataText) return;
  if (id <= state.lastEventId && state.lastEventId > 0) return; // 去重
  if (id > 0) state.lastEventId = id;
  state.frames.push({ id, event, dataText });
  let data = {};
  try { data = dataText ? JSON.parse(dataText) : {}; } catch { data = { _raw: dataText }; }
  dispatch(event, data);
}

function dispatch(event, data) {
  switch (event) {
    case "interaction.started":
      showProcessing("接收");
      break;
    case "interaction.phase":
      onPhase(data);
      break;
    case "interaction.clarification":
      onClarification(data);
      break;
    case "interaction.result":
      onResult(data);
      break;
    case "interaction.completed":
      onCompleted(data);
      break;
    case "interaction.failed":
      onFailed(data);
      break;
    case "interaction.cancelled":
      onCancelled(data);
      break;
    case "interaction.heartbeat":
      break; // 保活，忽略
    default:
      if (data && Object.keys(data).length) {
        appendBubble("system", `未知事件 ${event}`, data);
      }
  }
}

/* ---------------- 事件渲染 ---------------- */
let processingNode = null;

function showProcessing(stage) {
  hideEmptyHint();
  const node = document.createElement("div");
  node.className = "msg assistant";
  node.innerHTML = `
    <div class="phase-card">
      <div class="line"><span class="spinner"></span><span class="phase-text"></span></div>
      <div class="trail"></div>
      <details class="raw"><summary>查看原始事件</summary><pre></pre></details>
    </div>`;
  messagesEl.appendChild(node);
  processingNode = node;
  updateProcessing(stage || "接收");
  scrollBottom();
}

function updateProcessing(stage) {
  if (!processingNode) return;
  processingNode.querySelector(".phase-text").textContent = stage || "处理中…";
  if (processingNode.querySelector(".trail").children.length === 0 && state.frames.length > 1) {
    // 只显示阶段轨迹（从 interaction.phase 累积）
  }
}

function addTrail(stateName) {
  if (!processingNode) return;
  const label = STATE_LABEL[stateName] || stateName;
  const trail = processingNode.querySelector(".trail");
  const cur = trail.querySelector(".cur");
  if (cur) { cur.classList.remove("cur"); cur.classList.add("done"); }
  const span = document.createElement("span");
  span.className = "cur";
  span.textContent = label;
  trail.appendChild(span);
}

function onPhase(data) {
  if (!processingNode) showProcessing(null);
  addTrail(data.state || "");
  const st = data.stage || STATE_LABEL[data.state] || "";
  if (st) updateProcessing(st);
  // 阶段消息附带的原因/耗时给调试用
  const note = [data.reason, data.duration_ms != null ? `${data.duration_ms}ms` : null]
    .filter(Boolean).join(" · ");
  if (note && processingNode) processingNode.querySelector(".phase-text").textContent += `（${note}）`;
}

function onClarification(data) {
  if (processingNode) { processingNode.remove(); processingNode = null; }
  const node = appendBubble("assistant", data.question || "请补充信息");
  const chipRow = document.createElement("div");
  chipRow.className = "clar-chip";
  const missing = (data.missing || []).map(slotName);
  const ambiguous = (data.ambiguous || []).map(slotName);
  for (const t of [...missing, ...ambiguous].filter(Boolean)) {
    const c = document.createElement("span");
    c.textContent = t;
    chipRow.appendChild(c);
  }
  if (chipRow.children.length) node.querySelector(".bubble").appendChild(chipRow);
  scrollBottom();
}

function slotName(s) {
  const names = { time_range: "缺少时间范围", time_expression: "缺少时间条件",
    order_codes: "缺少订单号", plan_codes: "缺少生产单号", style_codes: "缺少款号",
    dept_names: "缺少小组/部门", employee_names: "缺少员工" };
  return names[s] || s;
}

/* 结果“不完整”原因：内部枚举串 → 可读文案（未登记的原样展示，不伪造） */
const INCOMPLETE_REASON_LABEL = {
  "metric_unavailable:quality_defective": "次品数暂无数据源",
  "metric_unavailable": "存在无数据源的指标列",
  "pagination_duplicate_page": "分页未取全：服务端忽略分页参数",
  "pagination_page_budget_exhausted": "分页未取全：超过翻页上限",
  "row_budget_exhausted": "结果超出行数上限",
};
function reasonLabel(r) { return r ? (INCOMPLETE_REASON_LABEL[r] || r) : ""; }

function onResult(data) {
  if (!processingNode) showProcessing("结果就绪");
  const msgNode = processingNode;
  msgNode.classList.add("result-card");
  msgNode.querySelector(".spinner").remove();
  const pc = msgNode.querySelector(".phase-card");
  pc.querySelector(".phase-text").textContent = "结果就绪";
  const cols = data.columns || [];
  const rowCount = data.row_count ?? "?";
  const incomplete = data.incomplete === true;
  const warnCols = cols.filter((c) => /delivery_warning|days_remaining/.test(c));

  const head = document.createElement("div");
  head.className = "head";
  head.innerHTML = `
    <span class="title">结果卡片</span>
    <span class="chip">${escapeHtml(data.capability_id || "")}</span>
    <span class="tag ${incomplete ? "warn" : "ok"}">${incomplete ? "不完整" : "完整"}</span>
    ${incomplete && data.incomplete_reason ? `<span class="tag warn">${escapeHtml(reasonLabel(data.incomplete_reason))}</span>` : ""}
    <span class="tag">${rowCount} 行</span>`;

  const colRow = document.createElement("div");
  colRow.className = "cols";
  for (const c of cols) {
    const s = document.createElement("span");
    s.textContent = c;
    if (warnCols.includes(c)) { s.classList.add("warn-col"); s.title = "异常数据高亮列（后端只下发标记，前端渲染）"; }
    colRow.appendChild(s);
  }

  const note = document.createElement("div");
  note.style.cssText = "font-size:11px;color:var(--muted);margin-top:8px;";
  note.textContent = "说明：SSE 只携带结果元数据（列定义/行数/完整性）；行级明细请点“导出报表”取 xlsx。";

  pc.appendChild(head);
  pc.appendChild(colRow);
  pc.appendChild(note);

  if (data.artifact_id) {
    const btn = document.createElement("button");
    btn.className = "export-btn";
    btn.textContent = "导出报表（xlsx）";
    btn.addEventListener("click", () => downloadArtifact(data.artifact_id, data.capability_id || "result", btn));
    pc.appendChild(btn);
  }
  refreshRaw();
  scrollBottom();
}

async function downloadArtifact(artifactId, capabilityId, btn) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "生成中…";
  try {
    const resp = await api(`/v1/artifacts/${artifactId}/download`);
    if (!resp.ok) {
      const err = await readError(resp);
      toast(`导出失败（HTTP ${resp.status}）：${err}。产物短时效，可点击快捷问题重新查询后再次导出。`);
      return;
    }
    const blob = await resp.blob();
    const cd = resp.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
    const filename = m ? decodeURIComponent(m[1]) : `factory-${capabilityId}-${Date.now()}.xlsx`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    toast(`导出请求异常：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function onCompleted(data) {
  const status = data.status || "completed";
  endProcessing();
  // clarifying 也是终态（追问收尾，等用户补充后开新一轮）：必须停流。
  finishTurn(status === "clarifying" ? "等待补充" : "完成");
}

function onFailed(data) {
  const cat = data.error_category || "unknown";
  endProcessing();
  const node = appendBubble("error", `查询未能完成。`, null, true);
  const tag = document.createElement("span");
  tag.className = "kind-tag";
  tag.textContent = cat;
  node.querySelector(".bubble").prepend(tag);
  // 友好文案以后端持久化消息为准，终态后 restoreMessages() 会用 kind=error 消息替换渲染
  finishTurn("失败");
}

function onCancelled(data) {
  endProcessing();
  finishTurn("已取消");
}

function finishTurn(label) {
  state.terminal = true;
  connState.textContent = label;
  connState.classList.add("live");
  quickWrap.classList.remove("hidden");
}

function endProcessing() {
  if (processingNode) {
    const pc = processingNode.querySelector(".phase-card");
    if (pc && !pc.querySelector(".head")) {
      // 无结果/追问内容的普通收尾：保留简洁过程信息
      pc.querySelector(".spinner")?.remove();
      const st = pc.querySelector(".phase-text");
      if (st) st.textContent += " → 结束";
    }
    refreshRaw();
    processingNode = null;
  }
}

function refreshRaw() {
  if (!processingNode) return;
  const pre = processingNode.querySelector("details.raw pre");
  if (pre) pre.textContent = state.frames.map((f) => `id: ${f.id}\nevent: ${f.event}\ndata: ${f.dataText}`).join("\n\n");
}

/* ---------------- 通用气泡 ---------------- */
function appendBubble(cls, text, payload, keepProcessing) {
  if (!keepProcessing && processingNode) { processingNode.remove(); processingNode = null; }
  hideEmptyHint();
  const node = document.createElement("div");
  node.className = `msg ${cls}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  node.appendChild(bubble);
  messagesEl.appendChild(node);
  scrollBottom();
  return node;
}

function hideEmptyHint() {
  const e = messagesEl.querySelector(".empty-state");
  if (e) e.remove();
}

/* ---------------- busy / 输入 ---------------- */
function setBusy(busy) {
  state.busy = busy;
  $("btn-send").disabled = busy;
  $("btn-cancel").hidden = !busy;
  inputEl.disabled = busy;
  if (busy) quickWrap.classList.add("hidden");
  connState.textContent = busy ? "处理中…" : "";
}

async function stopInteraction() {
  state.terminal = true;
  if (state.controller) state.controller.abort();
  if (state.interactionId) {
    try {
      await api(`/v1/interactions/${state.interactionId}/cancel`, { method: "POST" });
    } catch { /* ignore */ }
  }
  endProcessing();
  connState.textContent = "已取消";
  setBusy(false);
  await restoreMessages();
}

/* ---------------- 其它 ---------------- */
function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
}

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function checkHealth() {
  try {
    const resp = await fetch("/health/live");
    const body = await resp.json();
    healthPill.textContent = `后端 ${body.status} · v${body.version}`;
    healthPill.className = "health ok";
  } catch {
    healthPill.textContent = "后端不可达";
    healthPill.className = "health down";
  }
}

/* ---------------- 事件绑定与启动 ---------------- */
$("btn-back").addEventListener("click", exitToSelect);
$("btn-send").addEventListener("click", () => send());
$("btn-cancel").addEventListener("click", stopInteraction);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
inputEl.addEventListener("input", autosize);

checkHealth();
setInterval(checkHealth, 15_000);
loadRoles();
