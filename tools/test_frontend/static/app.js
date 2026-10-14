/* Factory Agent 四角色测试台 —— 前端逻辑
 *
 * 所有 /v1 请求都发给同源代理（tools/test_frontend/server.py），代理按
 * X-Dev-Role 头选择角色并注入 X-Factory-Credential。SSE 用 fetch 流读取
 * （EventSource 无法携带自定义头），断线按文档约定用 Last-Event-ID 续传。
 *
 * 会话数据全部服务端驱动（docs/api/会话接口-前端对接.md）：
 *   C1 GET  /v1/conversations          历史会话列表（服务端按 updated_at 倒序）
 *   C2 POST /v1/conversations          新建会话（幂等，复用已有会话直接回详情）
 *   C3 GET  /v1/conversations/{sid}    会话详情 = interactions + messages 整个会话流
 * 浏览器本地只存「当前会话指针」（fa_sid_<role>）做刷新恢复，不再存会话列表。
 */
"use strict";

const ROLE_HEADER = "X-Dev-Role";
const UPSTREAM_HEADER = "X-Dev-Upstream";
const SID_KEY = (role) => `fa_sid_${role}`;
const ADDR_KEY = "fa_backend_addr";
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
  sessions: [],    // C1 返回的会话摘要（服务端已按 updated_at 倒序，勿重排）
  interactionId: null,
  lastEventId: 0,
  terminal: false,
  busy: false,
  attempts: 0,
  frames: [],      // 本轮原始事件（用于“查看原始事件”）
  trail: [],       // 阶段轨迹
  thinking: null,  // interaction.thinking 累积块：{ seen, node, body, done }
  controller: null,
  drawerPinned: false, // 会话抽屉是否被点击固定
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

/* 后端地址配置：空 = 服务端默认；非空经代理 X-Dev-Upstream 头转发到该地址 */
function normalizeAddr(raw) {
  let s = (raw || "").trim();
  if (!s) return "";
  if (!/^https?:\/\//i.test(s)) s = "http://" + s;
  return s.replace(/\/+$/, "");
}

function backendAddr() {
  return normalizeAddr(localStorage.getItem(ADDR_KEY));
}

function upstreamHeaders(extra = {}) {
  const headers = Object.assign({}, extra);
  const addr = backendAddr();
  if (addr) headers[UPSTREAM_HEADER] = addr;
  return headers;
}

async function api(path, options = {}) {
  const headers = Object.assign(upstreamHeaders(), options.headers || {});
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

/* ---------------- 历史会话（服务端驱动：C1 / C2，见会话接口文档） ----------------
 * 会话列表的唯一来源是 C1：服务端已按 updated_at 倒序返回，前端按返回顺序渲染、
 * **不做任何重排**。标题是服务端派生值（首条提问截断 30 字），不可编辑；
 * 本期也没有删除接口，所以抽屉里不放重命名/删除入口。
 * 浏览器本地只留 fa_sid_<role> 作为「当前会话」指针（UI 记忆，可随时清掉）。
 * 旧版存在 localStorage 的 fa_sessions_<role> 列表在这里退役，进角色时清除。 */

async function fetchSessions() {
  try {
    const resp = await api("/v1/conversations?limit=100");
    if (!resp.ok) return false;
    const page = await resp.json();
    state.sessions = Array.isArray(page.items) ? page.items : [];
    return true;
  } catch {
    return false;
  }
}

async function refreshSessionList() {
  await fetchSessions();
  renderSessionList();
}

function sessionById(sid) {
  return state.sessions.find((s) => s.session_id === sid) || null;
}

function sessionTitle(s) {
  return (s && s.title) || "新对话";
}

function fmtTime(iso) {
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const d = new Date(ts);
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ---------------- 抽屉渲染与交互 ---------------- */
const drawerEl = () => $("session-drawer");

function drawerOpen() {
  clearTimeout(drawerCloseTimer);
  drawerEl().classList.add("open");
}

function drawerClose() {
  clearTimeout(drawerCloseTimer);
  drawerEl().classList.remove("open");
}

function scheduleDrawerClose() {
  if (state.drawerPinned) return;
  clearTimeout(drawerCloseTimer);
  drawerCloseTimer = setTimeout(drawerClose, 260); // 260ms 延迟防误触抖动
}

let drawerCloseTimer = null;

function renderSessionList() {
  const listEl = $("session-list");
  if (!state.role) return;
  listEl.innerHTML = "";
  if (!state.sessions.length) {
    listEl.innerHTML = `<div class="session-empty">暂无历史会话</div>`;
    return;
  }
  // C1 已按 updated_at 倒序：直接按返回顺序渲染，不排序、不倒转。
  for (const s of state.sessions) {
    const item = document.createElement("div");
    item.className = "session-item" + (s.session_id === state.sid ? " current" : "");
    item.title = s.session_id;
    // thinking 不展示推理原文，用占位文案（与主文档口径一致）
    const preview = s.last_message && s.last_message.kind === "thinking"
      ? "思考过程"
      : (s.last_message && s.last_message.text) || "";
    item.innerHTML = `
      <div class="s-title"></div>
      <div class="s-meta"></div>
      <div class="s-preview"></div>`;
    item.querySelector(".s-title").textContent = sessionTitle(s);
    const statusTag = s.last_status && s.last_status !== "completed"
      ? ` · ${STATE_LABEL[s.last_status] || s.last_status}`
      : "";
    item.querySelector(".s-meta").textContent =
      `${fmtTime(s.updated_at)} · ${s.message_count ?? 0} 条${statusTag}`;
    item.querySelector(".s-preview").textContent = preview;
    item.addEventListener("click", () => switchSession(s.session_id));
    listEl.appendChild(item);
  }
}

async function newSession() {
  if (!state.role) return;
  if (state.busy) { toast("请先停止当前请求再新建会话"); return; }
  // C2 新建会话（幂等）：session_id 由前端生成。连点/重试不会产生第二个会话；
  // 201 = 新建成功，200 = 复用已有会话（body 是它的完整详情，不清空历史）。
  const requested = `sess_${state.role}_${Date.now().toString(36)}`;
  let resp;
  try {
    resp = await api("/v1/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: requested }),
    });
  } catch (err) {
    toast(`新建会话请求异常：${err.message}`);
    checkHealth();
    return;
  }
  if (resp.status !== 200 && resp.status !== 201) {
    const err = await readError(resp);
    toast(`新建会话失败 HTTP ${resp.status}：${err}`);
    return;
  }
  const detail = await resp.json();
  state.sid = detail.conversation.session_id;
  localStorage.setItem(SID_KEY(state.role), state.sid);
  $("session-chip").textContent = state.sid;
  messagesEl.innerHTML = "";
  showEmptyHint();
  connState.textContent = "已连接";
  connState.classList.add("live");
  // 就地把新会话置顶进本地列表，省一次 C1（文档 §8.2 建议）
  state.sessions = state.sessions.filter((s) => s.session_id !== state.sid);
  state.sessions.unshift(detail.conversation);
  renderSessionList();
  drawerClose();
  inputEl.focus();
}

async function switchSession(sid) {
  if (!state.role || state.busy) { if (state.busy) toast("请先停止当前请求再切换会话"); return; }
  if (sid === state.sid) { drawerClose(); return; }
  state.sid = sid;
  localStorage.setItem(SID_KEY(state.role), sid);
  $("session-chip").textContent = sid;
  messagesEl.innerHTML = "";
  connState.textContent = "加载中…";
  connState.classList.remove("live");
  await restoreMessages();
  connState.textContent = "已连接";
  connState.classList.add("live");
  renderSessionList();
  drawerClose();
}

/* ---------------- 进入角色 / 恢复会话 ---------------- */
async function enterRole(code, name) {
  state.role = code;
  $("role-badge").textContent = code;
  $("role-badge").style.setProperty("--rc", ROLE_COLORS[code] || "#4c9aff");
  $("role-name").textContent = `${name}（${code}）`;
  $("session-chip").textContent = "…";
  viewSelect.hidden = true;
  viewChat.hidden = false;
  messagesEl.innerHTML = "";
  connState.textContent = "连接中…";
  connState.classList.remove("live");
  loadQuickQuestions();
  checkHealth();
  // 旧版「会话列表存 localStorage」退役：清掉遗留键，避免误读。
  localStorage.removeItem(`fa_sessions_${code}`);
  // C1 拉服务端会话列表；本地指针命中则恢复该会话，否则 C2 新建一个。
  await refreshSessionList();
  const pointer = localStorage.getItem(SID_KEY(code));
  if (pointer && sessionById(pointer)) {
    state.sid = pointer;
    $("session-chip").textContent = state.sid;
    await restoreMessages();
  } else {
    await newSession();
  }
  connState.textContent = "已连接";
  connState.classList.add("live");
  renderSessionList();
}

function exitToSelect() {
  if (state.busy) stopInteraction();
  drawerClose();
  state.drawerPinned = false;
  state.role = null;
  viewChat.hidden = true;
  viewSelect.hidden = false;
  messagesEl.innerHTML = "";
  quickWrap.classList.remove("hidden");
  loadRoles();
  checkHealth();
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

/* ---------------- 历史消息恢复（C3：会话详情 = 整个会话流） ----------------
 * C3 的 messages 已按 (created_at, interaction_id, sequence) 升序返回：
 * 直接按数组顺序渲染、不重排（sequence 每轮从 1 重新计数，绝不能当全局排序键）。
 * 单页 limit=200 是服务端上限，更长的会话用 next_cursor 继续翻完。 */
async function restoreMessages() {
  if (!state.sid) return;
  let cursor = null;
  const messages = [];
  try {
    for (;;) {
      const q = new URLSearchParams({ limit: "200" });
      if (cursor) q.set("cursor", cursor);
      const resp = await api(`/v1/conversations/${encodeURIComponent(state.sid)}?${q}`);
      if (resp.status === 404) {
        // 会话不存在或不属于当前身份（两者不可区分）：清指针，落到列表第一条或新建。
        localStorage.removeItem(SID_KEY(state.role));
        state.sessions = state.sessions.filter((s) => s.session_id !== state.sid);
        state.sid = null;
        const first = state.sessions[0];
        if (first) await switchSession(first.session_id);
        else await newSession();
        return;
      }
      if (!resp.ok) {
        if (!messages.length) showEmptyHint();
        return;
      }
      const detail = await resp.json();
      messages.push(...(detail.messages || []));
      cursor = detail.next_cursor;
      if (!cursor) break;
    }
  } catch {
    showEmptyHint();
    return;
  }
  messagesEl.innerHTML = "";
  if (!messages.length) {
    showEmptyHint();
    return;
  }
  // 每个 turn 只在末尾挂一次操作区：同一 interaction_id 会有多条消息
  // （thinking / phase / 结果），逐条挂会让一次请求冒出多个下载入口。
  const turnEnds = turnFinalIndexes(messages);
  messages.forEach((m, index) => renderRestoredMessage(m, turnEnds.has(index)));
  scrollBottom();
}

function showEmptyHint() {
  const div = document.createElement("div");
  div.className = "empty-state";
  div.textContent = "还没有对话记录。用下面的快捷问题或直接输入开始测试。";
  messagesEl.appendChild(div);
}

/* 一个 turn 的末尾消息：它的 interaction_id 与下一条不同（或已是最后一条）。
 * 只在末尾挂操作区，保证每个 turn 恰好一个链路报告入口。 */
function turnFinalIndexes(items) {
  const ends = new Set();
  for (let i = 0; i < items.length; i += 1) {
    const cur = items[i];
    if (!cur || !cur.interaction_id) continue;
    const next = items[i + 1];
    if (!next || next.interaction_id !== cur.interaction_id) ends.add(i);
  }
  return ends;
}

function renderRestoredMessage(m, isTurnEnd) {
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
    } else if (kind === "thinking") {
      // 契约 §4：恢复渲染默认收起；文本是本轮完整 transcript，payload 带耗时
      const details = document.createElement("details");
      details.className = "thinking done";
      const summary = document.createElement("summary");
      const elapsed = m.payload && typeof m.payload.elapsed_ms === "number"
        ? ` · ${m.payload.elapsed_ms}ms` : "";
      summary.textContent = `思考过程${elapsed}`;
      const body = document.createElement("div");
      body.className = "thinking-body";
      const line = document.createElement("div");
      line.className = "thinking-line";
      line.textContent = m.text;
      body.appendChild(line);
      details.appendChild(summary);
      details.appendChild(body);
      bubble.appendChild(details);
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
  // sequence 是「轮内」序号（每轮从 1 重新计数），只作调试展示，不参与任何排序
  meta.textContent = `轮内 #${m.sequence} · ${m.kind} · ${fmtTime(m.created_at)}`;
  node.appendChild(meta);
  // 链路报告按钮无条件渲染：这是内部测试台，不做 capabilities 探测。
  // 即使后端还没实现该路由，也要能点出真实报错，而不是让入口静默消失。
  if (isTurnEnd && m.role !== "user" && m.interaction_id) {
    node.appendChild(turnActions(m.interaction_id));
  }
  messagesEl.appendChild(node);
  return node;
}

/* 历史消息中的 result_table → 重建结果卡片（与实时渲染同一套函数）。
 * 需要消息 payload 携带卡片载荷；旧消息无 card 时回退为列元数据形态。 */
function renderRestoredResultCard(m, node) {
  const p = m.payload;
  if (!p || !Array.isArray(p.columns) || !p.columns.length) return false;
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

  const payloadCard = cardOf(p);
  card.appendChild(resultHead({
    title: payloadCard ? payloadCard.title : null,
    capabilityId: p.capability_id || "",
    incomplete: p.incomplete === true,
    incompleteReason: p.incomplete_reason,
    rowCount: p.row_count,
  }));

  if (payloadCard) {
    card.appendChild(renderCardBody(payloadCard));
  } else {
    card.appendChild(columnsRow(p.columns, p.column_titles));
  }

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
function send(textRaw, drill) {
  const text = (textRaw ?? inputEl.value).trim();
  if (!text || state.busy || !state.role) return;
  inputEl.value = "";
  autosize();
  appendUserMessage(text, drill);
  startInteraction(text, drill);
}

function appendUserMessage(text, drill) {
  const node = document.createElement("div");
  node.className = "msg user";
  node.innerHTML = `<div class="bubble"></div>`;
  node.querySelector(".bubble").textContent = text;
  if (drill) {
    const tag = document.createElement("span");
    tag.className = "kind-tag";
    tag.textContent = `drill → ${drill.capability_id}`;
    node.querySelector(".bubble").appendChild(document.createElement("br"));
    node.querySelector(".bubble").appendChild(tag);
  }
  messagesEl.appendChild(node);
  scrollBottom();
}

async function startInteraction(text, drill) {
  setBusy(true);
  state.frames = [];
  state.trail = [];
  state.thinking = null;
  state.lastEventId = 0;
  state.terminal = false;
  state.attempts = 0;
  try {
    const resp = await api(`/v1/sessions/${encodeURIComponent(state.sid)}/interactions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // 结构化下钻（2026-09-16 拍板）：卡片 actions 的点击载荷；drill 缺席时与旧版完全一致
      body: JSON.stringify(drill ? { text, drill } : { text }),
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
    checkHealth(); // 请求失败 → 探测一次后端连通性
  } finally {
    setBusy(false);
    await restoreMessages();
    // 一轮问答结束后重新拉 C1：让列表排序与预览与服务端同步（文档 §8.4 方案 a）
    await refreshSessionList();
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
    const headers = upstreamHeaders();
    if (state.lastEventId > 0) headers["Last-Event-ID"] = String(state.lastEventId);
    headers[ROLE_HEADER] = state.role;
    if (state.attempts > 0) connState.textContent = `重连中(${state.attempts})…`;

    try {
      const resp = await fetch(`/v1/interactions/${interactionId}/stream`, { headers, signal: ac.signal });
      if (!resp.ok) { toast(`SSE HTTP ${resp.status}`); break; }
      const reader = resp.body.getReader();
      for (;;) {
        // 读超时是「连接活着但没有任何字节」的兜底：后端跟随模式每 15s 发一次
        // 心跳，正常永远打不到这里；调大到 10 分钟是为了支持后端断点调试
        // （进程冻结期间无字节流动）。
        const { value, done } = await raceTimeout(reader.read(), 600_000, ac);
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
    case "interaction.progress":
      onProgress(data);
      break;
    case "interaction.thinking":
      onThinking(data);
      break;
    case "interaction.answer":
      onAnswer(data);
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

/* 阶段进行中：只换当前等待文案，不进阶段轨迹（轨迹由 phase 累积） */
const PROGRESS_STAGE = {
  parse_started: "解析中",
  authorize_started: "权限检查中",
  scope_resolution_started: "核对数据范围",
};

function onProgress(data) {
  if (!processingNode) showProcessing(null);
  updateProcessing(data.stage || PROGRESS_STAGE[data.reason] || "处理中…");
}

/* ---------- 思考过程（interaction.thinking）----------
 * 每个事件携带一“帧”（一句话），不是全文：seq 是本轮帧号，最后一帧 text 为空且
 * done=true（后端契约 §3.2）。块插在“处理中”卡片之前：帧在解析期就开始到，
 * 提前占位也让它在结果卡片之前、且取数结束卡片被移除时不受影响。 */
function onThinking(data) {
  if (!state.thinking) {
    hideEmptyHint();
    const node = document.createElement("div");
    node.className = "msg assistant";
    const details = document.createElement("details");
    details.className = "thinking";
    details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "思考过程";
    const body = document.createElement("div");
    body.className = "thinking-body";
    details.appendChild(summary);
    details.appendChild(body);
    node.appendChild(details);
    if (processingNode) messagesEl.insertBefore(node, processingNode);
    else messagesEl.appendChild(node);
    state.thinking = { seen: new Set(), node, body, details };
  }
  const block = state.thinking;
  const seq = Number(data.seq) || 0;
  if (seq > 0) {
    if (block.seen.has(seq)) return; // 断线重连重放：同一帧只渲染一次
    block.seen.add(seq);
  }
  const text = typeof data.text === "string" ? data.text : "";
  if (text) {
    const line = document.createElement("div");
    line.className = "thinking-line";
    line.textContent = text;
    block.body.appendChild(line);
  }
  if (data.truncated) {
    const note = document.createElement("div");
    note.className = "thinking-note";
    note.textContent = "推理内容过长，已截断（后端 truncated 标记）";
    block.body.appendChild(note);
  }
  if (data.done) {
    block.details.classList.add("done");
    block.details.open = false; // 契约 §4：完成后收起，需要时可展开
    if (typeof data.elapsed_ms === "number") {
      block.details.querySelector("summary").textContent = `思考过程 · ${data.elapsed_ms}ms`;
    }
  }
  scrollBottom();
}

/* ---------- 自由问答的最终回答（interaction.answer）----------
 * 只在 chat 路径出现：{text}，随后 interaction.completed 收尾。不处理的话会落进
 * “未知事件”，且回答文本要等终态后的 restoreMessages() 才可见。 */
function onAnswer(data) {
  if (processingNode) { processingNode.remove(); processingNode = null; }
  appendBubble("assistant", data.text || "");
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
  "metric_unavailable": "存在无数据源的指标列",
  "pagination_duplicate_page": "分页未取全：服务端忽略分页参数",
  "pagination_page_budget_exhausted": "分页未取全：超过翻页上限",
  "row_budget_exhausted": "结果超出行数上限",
  "upstream_invalid": "数据源返回错误，本次结果不完整",
  "upstream_unavailable": "数据源暂不可达，本次结果不完整",
  "upstream_timeout": "数据源响应超时，本次结果不完整",
  "upstream_rate_limited": "数据源限流，本次结果不完整",
};
function reasonLabel(r) { return r ? (INCOMPLETE_REASON_LABEL[r] || r) : ""; }

/* ---------- 结果卡片（card 载荷） ----------
 * 后端按 recipe 声明下发 KPI 区 / 明细预览 / 合计 / 高亮标记；实时事件与历史消息
 * 同构。card 为可选字段，忽略它时回退为旧的“列名 + 行数 + 导出”形态。 */

function resultHead({ title, capabilityId, incomplete, incompleteReason, rowCount }) {
  const head = document.createElement("div");
  head.className = "head";
  const titleEl = document.createElement("span");
  titleEl.className = "title";
  titleEl.textContent = title || "结果卡片";
  head.appendChild(titleEl);
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.textContent = capabilityId || "";
  head.appendChild(chip);
  const state = document.createElement("span");
  state.className = `tag ${incomplete ? "warn" : "ok"}`;
  state.textContent = incomplete ? "不完整" : "完整";
  head.appendChild(state);
  if (incomplete && incompleteReason) {
    const why = document.createElement("span");
    why.className = "tag warn";
    why.textContent = reasonLabel(incompleteReason);
    head.appendChild(why);
  }
  const rows = document.createElement("span");
  rows.className = "tag";
  rows.textContent = `${rowCount ?? "?"} 行`;
  head.appendChild(rows);
  return head;
}

/* 兼容形态：只有列定义、行数与导出入口，用于不含 card 的旧记录。 */
function columnsRow(cols, titles) {
  const colRow = document.createElement("div");
  colRow.className = "cols";
  const warnCols = cols.filter((c) => /delivery_warning|days_remaining/.test(c));
  for (const c of cols) {
    const s = document.createElement("span");
    s.textContent = (titles || {})[c] || c;
    if (warnCols.includes(c)) {
      s.classList.add("warn-col");
      s.title = "异常数据高亮列（后端只下发标记，前端渲染）";
    }
    colRow.appendChild(s);
  }
  return colRow;
}

function metricRow(entries, isTotal) {
  const row = document.createElement("div");
  row.className = isTotal ? "kpi-row kpi-totals" : "kpi-row";
  for (const item of entries) {
    const box = document.createElement("div");
    box.className = "kpi" + (item.tone === "alert" ? " alert" : "");
    const label = document.createElement("div");
    label.className = "kpi-label";
    label.textContent = item.label || item.column || "";
    const value = document.createElement("div");
    value.className = "kpi-value";
    if (item.unavailable === true) {
      value.classList.add("missing");
      value.textContent = "暂无数据源";
    } else if (item.value === null || item.value === undefined || item.value === "") {
      value.classList.add("missing");
      value.textContent = "—";
    } else {
      value.textContent = `${item.value}${item.unit || ""}`;
    }
    box.appendChild(label);
    box.appendChild(value);
    row.appendChild(box);
  }
  return row;
}

function buildTable(table, rows, actions) {
  const el = document.createElement("table");
  el.className = "card-table";
  const titles = table.column_titles || {};
  const marker = table.alert_marker || null;
  const markerIndex = marker ? table.columns.indexOf(marker.column) : -1;

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const name of table.columns) {
    const th = document.createElement("th");
    th.textContent = titles[name] || name;
    headRow.appendChild(th);
  }
  head.appendChild(headRow);
  el.appendChild(head);

  // 高亮标记：后端只给语义（列 + 取值），样式由前端决定。
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    if (markerIndex >= 0 && String(row[markerIndex]) === String(marker.equals)) {
      tr.className = "alert-row";
    }
    for (let i = 0; i < table.columns.length; i += 1) {
      const td = document.createElement("td");
      const cell = row[i];
      if (cell === null || cell === undefined) {
        td.classList.add("missing");
        td.textContent = "—";
      } else {
        td.textContent = String(cell);
      }
      tr.appendChild(td);
    }
    // 结构化下钻（D-4 拍板）：actions 的 bind_from 是「槽位键 → 列名」映射；
    // 点击行时从该行取列值填槽发起新一轮请求，点哪看哪、不经过 LLM 路由。
    for (const action of actions || []) {
      const slots = drillSlotsFromRow(action, table, row);
      if (slots) {
        tr.classList.add("row-drill");
        tr.title = `点击下钻：${action.label} → ${action.capability_id}`;
        tr.addEventListener("click", () => {
          if (!state.busy) sendDrill(action, slots);
        });
        break;
      }
    }
    body.appendChild(tr);
  }
  el.appendChild(body);
  return el;
}

/* 普通列表：整表一次性铺开，超过 preview_max_rows 的行不下发。 */
function plainTable(table, actions) {
  const box = document.createElement("div");
  box.className = "card-table-wrap";
  box.appendChild(buildTable(table, table.rows || [], actions));
  if (table.truncated === true) {
    const hint = document.createElement("div");
    hint.className = "card-hint";
    hint.textContent = `仅展示前 ${table.preview_max_rows} 行（共 ${table.total_rows} 行），完整数据请导出。`;
    box.appendChild(hint);
  }
  return box;
}

/* 分组名单（card.table.group_by 非空）：按组切换、组内展开成员。
 * 同组成员在 rows 里是连续的一段，段落顺序与 groups[] 一致，所以按
 * 每组的 row_count 顺序切片即可，不需要自己按值分组。 */
function groupedTable(table, actions) {
  const box = document.createElement("div");
  box.className = "card-table-wrap grouped";
  const groups = Array.isArray(table.groups) ? table.groups : [];
  const rows = table.rows || [];

  const slices = [];
  let cursor = 0;
  for (const entry of groups) {
    const size = Number(entry.row_count) || 0;
    slices.push(rows.slice(cursor, cursor + size));
    cursor += size;
  }

  const tabs = document.createElement("div");
  tabs.className = "card-groups";
  const panel = document.createElement("div");
  panel.className = "card-group-panel";

  const show = (index) => {
    [...tabs.children].forEach((btn, i) => btn.classList.toggle("active", i === index));
    panel.replaceChildren(buildTable(table, slices[index] || [], actions));
    const entry = groups[index];
    if (entry && entry.truncated === true) {
      const hint = document.createElement("div");
      hint.className = "card-hint";
      hint.textContent = `本组共 ${entry.total_rows} 人，仅展示前 ${entry.row_count} 人，完整名单请导出。`;
      panel.appendChild(hint);
    }
  };

  groups.forEach((entry, index) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-group-tab";
    // groups[].name（2026-09-16 协议增量）：服务端给的显示名优先，缺席回退原始编号
    btn.textContent = `${entry.name || entry.group}（${entry.total_rows}）`;
    btn.title = entry.truncated
      ? `本组共 ${entry.total_rows} 人，卡片内保留前 ${entry.row_count} 人`
      : `本组共 ${entry.total_rows} 人`;
    btn.addEventListener("click", () => show(index));
    tabs.appendChild(btn);
  });

  box.appendChild(tabs);
  if (groups.length) show(0);
  box.appendChild(panel);

  const dropped = Number(table.groups_total || 0) - groups.length;
  if (dropped > 0) {
    const hint = document.createElement("div");
    hint.className = "card-hint";
    hint.textContent = `共 ${table.groups_total} 组，卡片内展示前 ${groups.length} 组，完整名单请导出。`;
    box.appendChild(hint);
  }
  return box;
}

function previewTable(table, actions) {
  return table.group_by ? groupedTable(table, actions) : plainTable(table, actions);
}

function renderCardBody(card) {
  const wrap = document.createElement("div");
  wrap.className = "card-body";
  const actions = actionsOf(card);

  const metrics = Array.isArray(card.metrics) ? card.metrics : [];
  if (metrics.length) wrap.appendChild(metricRow(metrics, false));

  // card.chart（2026-09-16 协议增量）：bar + granularity，无数据时后端整个缺席
  const chart = card.chart;
  if (chart && Array.isArray(chart.points) && chart.points.length) {
    wrap.appendChild(chartBlock(chart));
  }

  const table = card.table;
  if (table && Array.isArray(table.columns) && table.columns.length) {
    wrap.appendChild(previewTable(table, actions));
  }

  const actionsRowEl = actionsRow(card);
  if (actionsRowEl) wrap.appendChild(actionsRowEl);

  const totals = Array.isArray(card.totals) ? card.totals : [];
  if (totals.length) wrap.appendChild(metricRow(totals, true));

  const missing = Array.isArray(card.unavailable_columns) ? card.unavailable_columns : [];
  if (missing.length) {
    const hint = document.createElement("div");
    hint.className = "card-hint";
    hint.textContent = `暂无数据源：${missing.join("、")}`;
    wrap.appendChild(hint);
  }
  const notes = Array.isArray(card.notes) ? card.notes : [];
  for (const note of notes) {
    const el = document.createElement("div");
    el.className = "card-note";
    el.textContent = note;
    wrap.appendChild(el);
  }
  return wrap;
}

function cardOf(payload) {
  const card = payload && payload.card;
  return card && typeof card === "object" && typeof card.kind === "string" ? card : null;
}

/* ---------- 卡片 chart 与下钻 actions（2026-09-16 协议增量） ---------- */

function actionsOf(card) {
  return Array.isArray(card.actions) ? card.actions : [];
}

/* actions 是纯提示性字段：忽略它零行为变化；这里把它变成可点的下钻入口。 */
function drillSlotsFromRow(action, table, row) {
  const bind = action.bind_from || {};
  const slots = {};
  for (const key of Object.keys(bind)) {
    const idx = table.columns.indexOf(bind[key]);
    if (idx < 0) return null;
    const v = row[idx];
    if (v === null || v === undefined || v === "") return null;
    // 槽位键与后端 DrillPayload 对齐：dept_ids 是数组，employee_uid 是单值
    slots[key] = key === "dept_ids" ? [String(v)] : String(v);
  }
  return slots;
}

function sendDrill(action, slots) {
  send(
    action.label,
    Object.assign({ capability_id: action.capability_id, dept_ids: [], employee_uid: null }, slots)
  );
}

/* actions 声明区：无需行值的入口（空 bind_from）直接可点；
 * 需要行值的入口在表格行上点击触发，按钮置灰提示。 */
function actionsRow(card) {
  const actions = actionsOf(card);
  if (!actions.length) return null;
  const row = document.createElement("div");
  row.className = "card-actions-row";
  const tip = document.createElement("span");
  tip.className = "card-actions-tip";
  tip.textContent = "下钻入口：";
  row.appendChild(tip);
  for (const action of actions) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "action-btn";
    b.textContent = action.label;
    const bindInfo = action.bind_from && Object.keys(action.bind_from).length
      ? `（点击表格行触发 · bind_from: ${JSON.stringify(action.bind_from)}）`
      : "";
    b.title = `drill → ${action.capability_id}${bindInfo}`;
    if (bindInfo) {
      b.disabled = true;
    } else {
      b.addEventListener("click", () => {
        if (!state.busy) sendDrill(action, {});
      });
    }
    row.appendChild(b);
  }
  return row;
}

/* 纯 CSS 柱状图：granularity 为 week 时标签是周起始日（服务端已聚合）。 */
function chartBlock(chart) {
  const box = document.createElement("div");
  box.className = "card-chart";
  const head = document.createElement("div");
  head.className = "chart-head";
  const kindLabel = chart.type === "bar" ? "柱状图" : chart.type;
  const unitLabel = chart.unit ? ` · 单位：${chart.unit}` : "";
  const grainLabel = chart.granularity === "week" ? " · 周粒度" : " · 日粒度";
  head.textContent = `${kindLabel}${unitLabel}${grainLabel}`;
  box.appendChild(head);

  const points = chart.points;
  const values = points.map((p) => {
    const n = Number(p.value);
    return Number.isFinite(n) ? n : 0;
  });
  const max = Math.max(...values, 0);
  const labelEvery = Math.max(1, Math.ceil(points.length / 14));
  const bars = document.createElement("div");
  bars.className = "chart-bars";
  points.forEach((p, i) => {
    const col = document.createElement("div");
    col.className = "chart-col";
    const bar = document.createElement("div");
    bar.className = "chart-bar";
    bar.style.height = max > 0 ? `${Math.max(2, Math.round((values[i] / max) * 100))}%` : "2%";
    bar.title = `${p.label} = ${p.value}`;
    const label = document.createElement("div");
    label.className = "chart-label";
    label.textContent = i % labelEvery === 0 || i === points.length - 1 ? p.label : "";
    col.appendChild(bar);
    col.appendChild(label);
    bars.appendChild(col);
  });
  box.appendChild(bars);
  return box;
}

function onResult(data) {
  if (!processingNode) showProcessing("结果就绪");
  if (data.answer) {
    // LLM 组织的结果回答（空窗口时为“没有查询到相关记录”等文案）
    appendBubble("assistant", data.answer, null, true);
  }
  const msgNode = processingNode;
  msgNode.classList.add("result-card");
  msgNode.querySelector(".spinner").remove();
  const pc = msgNode.querySelector(".phase-card");
  pc.querySelector(".phase-text").textContent = "结果就绪";
  const cols = data.columns || [];
  const titles = data.column_titles || {};
  const card = cardOf(data);

  pc.appendChild(resultHead({
    title: card ? card.title : null,
    capabilityId: data.capability_id || "",
    incomplete: data.incomplete === true,
    incompleteReason: data.incomplete_reason,
    rowCount: data.row_count,
  }));

  if (card) {
    // 卡片载荷：KPI 区 + 明细预览 + 合计 + 高亮（行级明细只在预览行内）
    pc.appendChild(renderCardBody(card));
  } else {
    // 旧形态兼容：只有列定义、行数与导出入口
    pc.appendChild(columnsRow(cols, titles));
    const note = document.createElement("div");
    note.style.cssText = "font-size:11px;color:var(--muted);margin-top:8px;";
    note.textContent = "说明：SSE 只携带结果元数据（列定义/行数/完整性）；行级明细请点“导出报表”取 xlsx。";
    pc.appendChild(note);
  }

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
    const filename = filenameFromDisposition(cd, `factory-${capabilityId}-${Date.now()}.xlsx`);
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

/* ---------- 链路追踪报告（trace.html） ---------- */

/* 每个 turn 末尾的操作区。链路报告按钮**不做环境/能力判断**：这是内部测试台，
 * 必须始终有这个入口；点不动时也要能看到后端真实返回，而不是入口静默消失。 */
function turnActions(interactionId) {
  const row = document.createElement("div");
  row.className = "turn-actions";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "trace-btn";
  btn.textContent = "下载链路报告（HTML）";
  btn.title = `链路追踪报告：GET /v1/interactions/${interactionId}/trace.html`;
  btn.addEventListener("click", () => downloadTraceReport(interactionId, btn));
  row.appendChild(btn);
  return row;
}

/* RFC 6266：filename*（RFC 5987，可带中文）优先于纯 ASCII 的 filename。
 * 后端 trace.html 两个都给，中文文件名只存在于 filename* 里。 */
function filenameFromDisposition(disposition, fallback) {
  const extended = disposition.match(/filename\*=UTF-8'[^']*'([^;]+)/i);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      /* 编码坏了就退到 ASCII 名，不要因此让整次下载失败 */
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1].trim() : fallback;
}

async function downloadTraceReport(interactionId, btn) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "生成中…";
  try {
    // 必须走 fetch + blob：X-Factory-Credential 是请求头，导航跳转带不上。
    const resp = await api(`/v1/interactions/${encodeURIComponent(interactionId)}/trace.html`);
    if (!resp.ok) {
      const err = await readError(resp);
      toast(`链路报告获取失败（HTTP ${resp.status}）：${err}`);
      return;
    }
    const blob = await resp.blob();
    const cd = resp.headers.get("Content-Disposition") || "";
    const filename = filenameFromDisposition(cd, `链路追踪报告-${interactionId}.html`);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    toast(`链路报告请求异常：${err.message}`);
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
  const friendly = (data.message || "").trim();
  const node = appendBubble("error", friendly || `查询未能完成。`, null, true);
  const tag = document.createElement("span");
  tag.className = "kind-tag";
  tag.textContent = cat;
  node.querySelector(".bubble").prepend(tag);
  // 终态事件携带 message（如越权拒绝文案）时直接展示；否则终态后 restoreMessages() 用 kind=error 消息替换渲染
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
  await refreshSessionList();
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

/* 健康探测按需执行：页面首载、进入/退出对话页、请求失败、测试连接。
 * 不做定时轮询，避免刷屏后端日志。 */
async function checkHealth() {
  try {
    const resp = await fetch("/health/live", { headers: upstreamHeaders() });
    const body = await resp.json();
    healthPill.textContent = `后端 ${body.status} · v${body.version}`;
    healthPill.className = "health ok";
  } catch {
    healthPill.textContent = "后端不可达";
    healthPill.className = "health down";
  }
}

/* ---------------- 首页后端地址配置 ---------------- */
function setCfgStatus(text, cls) {
  const el = $("cfg-status");
  el.textContent = text;
  el.className = "cfg-status" + (cls ? ` ${cls}` : "");
}

function bindBackendConfig() {
  const input = $("cfg-addr");
  input.value = backendAddr();
  fetch("/api/config")
    .then((r) => r.json())
    .then((c) => {
      input.placeholder = `默认：${c.upstream_default}（可填 IP:端口 或完整 URL）`;
    })
    .catch(() => {});
  $("cfg-save").addEventListener("click", () => {
    const v = normalizeAddr(input.value);
    if (v) localStorage.setItem(ADDR_KEY, v);
    else localStorage.removeItem(ADDR_KEY);
    input.value = v;
    setCfgStatus(v ? `已保存：${v}` : "已清除，恢复服务端默认地址", "ok");
    checkHealth();
  });
  $("cfg-test").addEventListener("click", async () => {
    const v = normalizeAddr(input.value); // 测试输入框中的地址（未保存也可测）
    const btn = $("cfg-test");
    btn.disabled = true;
    setCfgStatus("测试中…", "");
    try {
      const headers = v ? { [UPSTREAM_HEADER]: v } : {};
      const resp = await fetch("/health/live", { headers });
      const body = await resp.json();
      setCfgStatus(`连接成功 · ${body.status} · v${body.version}`, "ok");
    } catch {
      setCfgStatus("连接失败：后端不可达", "err");
    } finally {
      btn.disabled = false;
    }
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("cfg-save").click();
  });
}

/* ---------------- 事件绑定与启动 ---------------- */
$("btn-back").addEventListener("click", exitToSelect);
$("btn-send").addEventListener("click", () => send());
$("btn-cancel").addEventListener("click", stopInteraction);
$("btn-new-session").addEventListener("click", newSession);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
inputEl.addEventListener("input", autosize);

/* 抽屉：悬停左边缘滑出，移开 260ms 后收起；点击触发条可固定/解除固定 */
const drawerHover = $("drawer-hover");
drawerHover.addEventListener("mouseenter", () => { clearTimeout(drawerCloseTimer); drawerOpen(); });
drawerHover.addEventListener("mouseleave", scheduleDrawerClose);
drawerEl().addEventListener("mouseenter", () => clearTimeout(drawerCloseTimer));
drawerEl().addEventListener("mouseleave", scheduleDrawerClose);
drawerHover.addEventListener("click", () => {
  state.drawerPinned = !state.drawerPinned;
  if (state.drawerPinned) drawerOpen();
  else scheduleDrawerClose();
});
document.addEventListener("click", (e) => {
  if (state.drawerPinned && !drawerEl().contains(e.target) && !drawerHover.contains(e.target)) {
    state.drawerPinned = false;
    drawerClose();
  }
});

checkHealth();
bindBackendConfig();
loadRoles();
