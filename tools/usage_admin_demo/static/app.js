/* usage-admin 运营看板 Demo —— 前端逻辑
 * 所有 /admin/v1 请求发给同源代理（tools/usage_admin_demo/server.py），
 * 由代理注入 Authorization: Bearer；浏览器端不接触 token。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const COLORS = { c1: "#4c9aff", c2: "#2aa198", c3: "#e0a03c", c4: "#9a7bd8", c5: "#e5534b" };
const MES_LABELS = { output: "产量", payroll: "工资", order: "订单", other: "其他" };
const CAT_COLORS = ["#4c9aff", "#2aa198", "#e0a03c", "#9a7bd8", "#e5534b"];

const q = new URLSearchParams(location.search);
const state = {
  start: null, end: null,
  tenant: "",            // 当前筛选的 app_key
  byTenantOffset: 0, byTenantTotal: 0,
  regOffset: 0, regTotal: 0,
  tsCache: null, tsSeries: "tokens",
};

/* ---------- utils ---------- */
function iso(d) { return d.toISOString().replace(/\.\d{3}Z$/, "Z"); }
function fmt(n) {
  if (n == null) return "—";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
  if (n >= 1000) return (n / 1e3).toFixed(1) + " k";
  return Number(n).toLocaleString("zh-CN");
}
function fmtMs(ms) {
  if (ms == null) return "—";
  return ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms) + " ms";
}
function fmtDt(s) { return s ? String(s).replace("T", " ").replace(/\.\d+Z$/, " UTC").replace("Z", " UTC") : "—"; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

let toastTimer;
function toast(text, ms = 4000) {
  const el = $("toast");
  el.textContent = text; el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

async function api(path, options = {}) {
  const resp = await fetch(path, Object.assign({}, options, {
    headers: Object.assign({}, options.headers || {}),
  }));
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body.detail && typeof body.detail === "string") detail = body.detail;
      if (resp.status === 401) detail = "凭据未配置或无效（USAGE_ADMIN_API_TOKEN）";
      if (resp.status === 403) detail = "当前角色无权限执行该操作";
    } catch { /* ignore */ }
    const err = new Error(detail); err.status = resp.status; throw err;
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function qs(params) {
  const p = new URLSearchParams(params);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/* ---------- date defaults ---------- */
function setDefaultRange() {
  const end = new Date(); end.setUTCHours(0, 0, 0, 0); end.setUTCDate(end.getUTCDate() + 1);
  const start = new Date(end); start.setUTCDate(start.getUTCDate() - 30);
  state.start = iso(start).slice(0, 10);
  state.end = iso(end).slice(0, 10);
  $("start").value = state.start;
  $("end").value = state.end;
}

/* ---------- tab ---------- */
document.querySelectorAll(".tab").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const name = b.dataset.tab;
    $("tab-overview").hidden = name !== "overview";
    $("tab-registry").hidden = name !== "registry";
    if (name === "overview") refreshAll();
    if (name === "registry") loadRegistry();
  });
});

/* ---------- boot ---------- */
async function boot() {
  setDefaultRange();
  bindFilters();
  await checkConn();
  await loadTenantOptions();
  await refreshAll();
  if (state.tenantOptions.length === 0) $("tenant-filter").disabled = true;
}
function bindFilters() {
  $("start").addEventListener("change", () => { state.start = $("start").value; });
  $("end").addEventListener("change", () => { state.end = $("end").value; });
  $("tenant-filter").addEventListener("change", () => {
    state.tenant = $("tenant-filter").value;
    state.byTenantOffset = 0;
  });
  $("btn-refresh").addEventListener("click", refreshAll);
  $("page-prev").addEventListener("click", () => { state.byTenantOffset = Math.max(0, state.byTenantOffset - 20); loadByTenant(); });
  $("page-next").addEventListener("click", () => { state.byTenantOffset += 20; loadByTenant(); });
  $("btn-reg-create").addEventListener("click", createRegistry);
}
async function checkConn() {
  try {
    const r = await api("/api/status");
    const pill = $("conn");
    if (r.token_configured) { pill.textContent = `已连接 · ${r.upstream}`; pill.className = "pill ok"; }
    else { pill.textContent = "Token 未配置"; pill.className = "pill bad"; }
  } catch {
    const pill = $("conn"); pill.textContent = "代理不可达"; pill.className = "pill bad";
  }
}

async function loadTenantOptions() {
  try {
    const sel = $("tenant-filter");
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "全部工厂";
    sel.appendChild(opt);
    const list = await api(`/admin/v1/tenants${qs(rangeParams())}`);
    state.tenantOptions = list || [];
    for (const k of state.tenantOptions) {
      const o = document.createElement("option");
      o.value = k; o.textContent = k;
      sel.appendChild(o);
    }
  } catch (err) { /* 无数据时保持空 */ state.tenantOptions = []; }
}
function rangeParams() {
  return { start: `${state.start}T00:00:00Z`, end: `${state.end}T00:00:00Z` };
}
function maybeTenant(params) {
  if (state.tenant) params.app_key = state.tenant;
  return params;
}

/* ---------- 总览 ---------- */
async function refreshAll() {
  await Promise.all([loadSummary(), loadTimeseries(), loadMesCats(), loadMesFailures(), loadCaps(), loadErrs(), loadByTenant()]);
}

async function loadSummary() {
  const kpi = $("kpis");
  kpi.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const s = await api(`/admin/v1/usage/summary${qs(rangeParams())}`);
    const t = s.tokens || {};
    const d = s.durations || {};
    const totalTokens = (t.prompt_tokens || 0) + (t.completion_tokens || 0) + (t.cached_tokens || 0) + (t.reasoning_tokens || 0);
    // 真实后端把状态码点分键放在 status 内（如 "status.completed"）。
    const st = s.status || {};
    const sc = (k) => st[`status.${k}`] ?? st[k] ?? 0;
    const cards = [
      ["总查询次数（F1.7）", fmt(s.questions), `${fmt(s.users)} 活跃用户`],
      ["完成率", `${s.valid_questions && s.questions ? (100 * s.valid_questions / s.questions).toFixed(1) : "—"}%`, `有效 ${fmt(s.valid_questions)}`],
      ["状态分布", "", `完成 ${fmt(sc("completed"))} · 失败 ${fmt(sc("failed"))} · 取消 ${fmt(sc("cancelled"))} · 拒绝 ${fmt(sc("rejected"))}`],
      ["总 Token（F1.6）", fmt(totalTokens), `prompt ${fmt(t.prompt_tokens)} · completion ${fmt(t.completion_tokens)}`],
      ["LLM 调用", fmt(s.llm_logical_calls), `物理尝试 ${fmt(s.llm_physical_attempts)}`],
      ["端到端耗时", fmtMs(d.e2e_duration_ms?.p50_ms), `p95 ${fmtMs(d.e2e_duration_ms?.p95_ms)} · p99 ${fmtMs(d.e2e_duration_ms?.p99_ms)}`],
      ["MES 耗时", fmtMs(d.mes_duration_ms?.p50_ms), `p95 ${fmtMs(d.mes_duration_ms?.p95_ms)}`],
      ["LLM 耗时", fmtMs(d.llm_duration_ms?.p50_ms), `p95 ${fmtMs(d.llm_duration_ms?.p95_ms)}`],
    ];
    kpi.innerHTML = cards.map(([k, v, sub]) => `
      <div class="kpi"><div class="k">${esc(k)}</div>
      <div class="v">${v}${k === "状态分布" ? "<small></small>" : ""}</div>
      <div class="s">${esc(sub)}</div></div>`).join("");
    if (!s.questions && !totalTokens) noteEmpty(kpi, "等待计量事件入库（生产环境每次问答自动写入）");
  } catch (err) {
    kpi.innerHTML = `<div class="empty">summary 加载失败：${esc(err.message)}</div>`;
  }
}
function noteEmpty(el, text) {
  const n = document.createElement("div");
  n.className = "muted";
  n.style.marginTop = "6px";
  n.textContent = text;
  el.appendChild(n);
}

async function loadTimeseries() {
  const params = Object.assign(rangeParams(), {
    granularity: "day",
    metrics: "users,questions,valid_questions,prompt_tokens,completion_tokens",
  });
  try {
    const data = await api(`/admin/v1/usage/timeseries${qs(params)}`);
    state.tsCache = data.points || [];
    renderTsLegend();
    drawTs();
  } catch (err) {
    $("ts-chart").innerHTML = `<div class="empty">timeseries 加载失败：${esc(err.message)}</div>`;
  }
}
function renderTsLegend() {
  const seriesDefs = {
    tokens: [["prompt_tokens", "Prompt Token", "#4c9aff"], ["completion_tokens", "Completion Token", "#2aa198"]],
    questions: [["questions", "问答数", "#e0a03c"]],
    users: [["users", "活跃用户", "#9a7bd8"]],
    valid: [["valid_questions", "有效问答", "#3fb98a"]],
  };
  const box = $("ts-legend");
  box.innerHTML = "";
  for (const [key, [m, label, color]] of Object.entries(seriesDefs)) {
    const b = document.createElement("span");
    b.style.cursor = "pointer";
    b.style.opacity = state.tsSeries === key ? "1" : ".55";
    b.style.setProperty("--c", color);
    b.textContent = label;
    b.addEventListener("click", () => { state.tsSeries = key; renderTsLegend(); drawTs(); });
    box.appendChild(b);
  }
}
function drawTs() {
  const box = $("ts-chart");
  const points = state.tsCache || [];
  if (!points.length) { box.innerHTML = `<div class="empty">暂无时间序列数据</div>`; return; }
  const defs = {
    tokens: [["prompt_tokens", "#4c9aff"], ["completion_tokens", "#2aa198"]],
    questions: [["questions", "#e0a03c"]],
    users: [["users", "#9a7bd8"]],
    valid: [["valid_questions", "#3fb98a"]],
  };
  const metrics = defs[state.tsSeries];
  const W = 900, H = 240, PAD = { l: 46, r: 16, t: 14, b: 26 };
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const xs = points.map((_, i) => PAD.l + (points.length === 1 ? iw / 2 : (i * iw) / (points.length - 1)));
  let maxV = 1;
  for (const p of points) for (const [m] of metrics) maxV = Math.max(maxV, Number(p.metrics?.[m] ?? 0));
  maxV = niceMax(maxV);
  const y = (v) => PAD.t + ih - (v / maxV) * ih;

  let svg = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
  for (let g = 0; g <= 4; g++) {
    const yy = PAD.t + (ih * g) / 4;
    svg += `<line x1="${PAD.l}" y1="${yy}" x2="${W - PAD.r}" y2="${yy}" stroke="#ffffff14"/>`;
    svg += `<text x="${PAD.l - 6}" y="${yy + 4}" fill="#9aa2b1" font-size="10" text-anchor="end">${fmt((maxV * (1 - g / 4)))}</text>`;
  }
  const ticks = Math.min(points.length, 8);
  for (let i = 0; i < ticks; i++) {
    const idx = Math.floor((i * (points.length - 1)) / Math.max(1, ticks - 1));
    const x = xs[idx];
    svg += `<text x="${x}" y="${H - 8}" fill="#9aa2b1" font-size="10" text-anchor="middle">${(points[idx].bucket || "").slice(5, 10)}</text>`;
  }
  for (const [m, color] of metrics) {
    const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${xs[i]},${y(Number(p.metrics?.[m] ?? 0))}`).join(" ");
    svg += `<path d="${path}" fill="none" stroke="${color}" stroke-width="2"/>`;
    for (let i = 0; i < points.length; i++) {
      const v = Number(points[i].metrics?.[m] ?? 0);
      if (v > 0 || i % Math.ceil(points.length / 40) === 0) {
        svg += `<circle cx="${xs[i]}" cy="${y(v)}" r="2" fill="${color}"><title>${points[i].bucket} ${m}=${fmt(v)}</title></circle>`;
      }
    }
  }
  svg += "</svg>";
  box.innerHTML = svg;
}
function niceMax(v) {
  const mag = Math.pow(10, Math.floor(Math.log10(v || 1)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * mag) return m * mag;
  return 10 * mag;
}

async function loadMesCats() {
  try {
    const d = await api(`/admin/v1/usage/mes-categories${qs(maybeTenant(rangeParams()))}`);
    renderBars($("mes-cat"), [
      ["output", "产量查询", d.categories?.output, "#4c9aff"],
      ["payroll", "工资查询", d.categories?.payroll, "#2aa198"],
      ["order", "订单进度", d.categories?.order, "#e0a03c"],
      ["other", "其他（认证/基础/吊挂）", d.categories?.other, "#9a7bd8"],
    ], d.total);
    $("mes-cat-note").textContent = `四类之和 = 成功 MES 调用 ${fmt(d.total)} 次`;
  } catch (err) { $("mes-cat").innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}
async function loadMesFailures() {
  try {
    const d = await api(`/admin/v1/usage/mes-failures${qs(maybeTenant(rangeParams()))}`);
    renderBars($("mes-fail"), [
      ["output", "产量", d.categories?.output ?? 0, "#4c9aff"],
      ["payroll", "工资", d.categories?.payroll ?? 0, "#2aa198"],
      ["order", "订单", d.categories?.order ?? 0, "#e0a03c"],
      ["other", "其他", d.categories?.other ?? 0, "#9a7bd8"],
    ], d.total);
    const errs = Object.entries(d.by_error || {}).map(([k, v]) => `${esc(k)} ${fmt(v)}`).join(" · ");
    $("mes-fail-note").textContent = `失败合计 ${fmt(d.total)} 次${errs ? ` ｜ 按错误：${errs}` : ""}`;
  } catch (err) { $("mes-fail").innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}
function renderBars(el, rows, total) {
  const max = Math.max(1, ...rows.map((r) => r[2] || 0));
  el.innerHTML = rows.map(([key, label, val, color]) => `
    <div class="bar-row">
      <span>${esc(label)}</span>
      <div class="track"><div class="fill" style="width:${(100 * (val || 0)) / max}%;background:${color}"></div></div>
      <span class="num">${fmt(val)}${total ? `（${((100 * (val || 0)) / total).toFixed(1)}%）` : ""}</span>
    </div>`).join("");
  if (!total) el.insertAdjacentHTML("afterend", `<div class="muted" style="margin-top:6px">暂无失败记录</div>`);
}

async function loadCaps() {
  await loadValues($("caps"), "/admin/v1/usage/capabilities", "暂无按能力分布数据");
}
async function loadErrs() {
  await loadValues($("errs"), "/admin/v1/usage/errors", "暂无错误类别数据");
}
async function loadValues(el, path, emptyText) {
  try {
    const d = await api(`${path}${qs(rangeParams())}`);
    const entries = Object.entries(d.values || {}).sort((a, b) => b[1] - a[1]).slice(0, 14);
    if (!entries.length) { el.innerHTML = `<div class="empty">${emptyText}</div>`; return; }
    el.innerHTML = entries.map(([k, v], i) => {
      const color = CAT_COLORS[i % CAT_COLORS.length];
      return `<span class="chip">${esc(k)} <b>${fmt(v)}</b> <span style="color:${color}">${(100 * v / entries[0][1]).toFixed(0)}%</span></span>`;
    }).join("");
  } catch (err) { el.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}

async function loadByTenant() {
  const tbody = $("by-tenant").querySelector("tbody");
  try {
    const params = Object.assign(rangeParams(), { limit: "20", offset: String(state.byTenantOffset) });
    maybeTenant(params);
    const d = await api(`/admin/v1/usage/by-tenant${qs(params)}`);
    state.byTenantTotal = d.total || 0;
    const rows = d.items || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="10"><div class="empty">暂无工厂用量（等待计量事件入库）</div></td></tr>`;
    } else {
      tbody.innerHTML = rows.map((r) => `
        <tr>
          <td>${esc(r.tenant_name || "—")}</td>
          <td><code>${esc(r.app_key)}</code></td>
          <td><span class="status ${r.status === "active" ? "active" : "disabled"}">${r.status === "active" ? "启用" : "停用"}</span></td>
          <td class="num">${fmt(r.token_total)}</td>
          <td class="num">${fmt(r.question_count)}</td>
          <td class="num">${fmt(r.mes_output)}</td>
          <td class="num">${fmt(r.mes_payroll)}</td>
          <td class="num">${fmt(r.mes_order)}</td>
          <td class="num">${fmt(r.mes_other)}</td>
          <td>${fmtDt(r.last_usage_at)}</td>
        </tr>`).join("");
    }
    const from = state.byTenantTotal ? state.byTenantOffset + 1 : 0;
    const to = Math.min(state.byTenantOffset + 20, state.byTenantTotal);
    $("page-info").textContent = state.byTenantTotal ? `第 ${from}–${to} 条 / 共 ${state.byTenantTotal} 条` : "无数据";
    $("page-prev").disabled = state.byTenantOffset <= 0;
    $("page-next").disabled = state.byTenantOffset + 20 >= state.byTenantTotal;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="10"><div class="empty">加载失败：${esc(err.message)}</div></td></tr>`;
  }
}

/* ---------- 工厂账户注册表 ---------- */
async function loadRegistry() {
  const tbody = $("reg-body");
  try {
    const d = await api(`/admin/v1/tenants/registry${qs({ limit: "20", offset: String(state.regOffset) })}`);
    state.regTotal = d.total || 0;
    const rows = d.items || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="6"><div class="empty">注册表为空——通过上方表单新增第一个工厂账户</div></td></tr>`;
    } else {
      tbody.innerHTML = rows.map((r) => `
        <tr>
          <td>${esc(r.tenant_name)}</td>
          <td><code>${esc(r.app_key)}</code></td>
          <td><span class="status ${r.status === "active" ? "active" : "disabled"}">${r.status === "active" ? "启用" : "停用"}</span></td>
          <td>${fmtDt(r.created_at)}</td>
          <td>${fmtDt(r.updated_at)}</td>
          <td class="row-actions">
            ${r.status === "active"
              ? `<button class="ghost danger" data-act="disable" data-key="${esc(r.app_key)}" data-name="${esc(r.tenant_name)}">停用</button>`
              : `<button class="ghost" data-act="enable" data-key="${esc(r.app_key)}" data-name="${esc(r.tenant_name)}">启用</button>`}
          </td>
        </tr>`).join("");
      tbody.querySelectorAll("[data-act]").forEach((b) =>
        b.addEventListener("click", () => toggleRegistry(b.dataset.act, b.dataset.key, b.dataset.name)));
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty">加载失败：${esc(err.message)}</div></td></tr>`;
  }
}
async function toggleRegistry(act, appKey, name) {
  try {
    if (act === "disable") await api(`/admin/v1/tenants/registry/${encodeURIComponent(appKey)}`, { method: "DELETE" });
    else await api(`/admin/v1/tenants/registry/${encodeURIComponent(appKey)}/enable`, { method: "POST" });
    toast(`${name} 已${act === "disable" ? "停用" : "启用"}`);
    loadRegistry();
  } catch (err) { toast(`操作失败：${err.message}`); }
}
async function createRegistry() {
  const name = $("reg-name").value.trim();
  if (!name) { toast("请填写工厂名称"); return; }
  try {
    const resp = await fetch("/admin/v1/tenants/registry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_name: name, status: $("reg-status").value }),
    });
    const body = resp.ok ? await resp.json() : null;
    if (!resp.ok || !body) {
      let detail = `HTTP ${resp.status}`;
      try { detail = body?.detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }
    toast(`已创建：${body.tenant_name}。明文 AppKey（仅此一次）：${body.app_key}`, 8000);
    $("reg-name").value = "";
    loadRegistry();
  } catch (err) { toast(`创建失败：${err.message}`); }
}

boot();
