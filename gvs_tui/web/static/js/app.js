// gvs web — main module: state, WebSocket, event handling, wiring.

import { api, fmtTokens, fmtElapsed } from "./api.js";
import * as R from "./render.js";

const $ = (s) => document.querySelector(s);

// Deep links: ?tab=overview|timeline|artifact  and  ?call=<index> (timeline).
const params = new URLSearchParams(location.search);
const wantedTab = params.get("tab");
let pendingCall = params.get("call") != null ? Number(params.get("call")) : null;

const S = {
  connected: false,
  config: null,
  run: null,
  current: null,
  runs: [],
  tab: "overview",
  detail: null,   // index of the call being inspected in the timeline tab
  log: [],
};

const els = {
  pill: $("#status-pill"),
  label: $("#status-label"),
  stats: $("#run-stats"),
  btnRun: $("#btn-run"),
  btnPause: $("#btn-pause"),
  btnResume: $("#btn-resume"),
  btnStop: $("#btn-stop"),
  btnNew: $("#btn-new"),
  btnRuns: $("#btn-runs"),
  chatLog: $("#chat-log"),
  chatText: $("#chat-text"),
  chatModel: $("#chat-model"),
  overview: $("#tab-overview"),
  timeline: $("#tab-timeline"),
  artifact: $("#tab-artifact"),
  files: $("#tab-files"),
  runsList: $("#runs-list"),
  conn: $("#conn-dot"),
  toast: $("#toast"),
  modalSettings: $("#modal-settings"),
  modalRuns: $("#modal-runs"),
  connResult: $("#conn-result"),
  cfgModels: $("#cfg-models"),
};

/* ---------------- websocket ---------------- */

let ws = null;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws`);
  } catch {
    setTimeout(connect, 2000);
    return;
  }
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    try { handleEvent(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
}

function setConn(on) {
  S.connected = on;
  els.conn.classList.toggle("on", on);
  els.conn.classList.toggle("off", !on);
  els.conn.title = on ? "connected" : "reconnecting…";
}

/* ---------------- event handling ---------------- */

function handleEvent(ev) {
  switch (ev.type) {
    case "state": applyState(ev); break;
    case "init": onInit(ev); break;
    case "call": onCall(ev); break;
    case "ledger": onLedger(ev); break;
    case "verify": onVerify(ev); break;
    case "status": onStatus(ev); break;
    case "chat_token": onChatToken(ev); break;
    case "chat_message": onChatMessage(ev); break;
    case "chat_error": onChatError(ev); break;
    case "log": onLog(ev); break;
    case "done": onDone(ev); break;
    case "error": toast(ev.message || "unknown error"); break;
  }
}

function applyState(ev) {
  S.config = ev.config;
  S.run = ev.run;
  S.current = ev.current;
  S.runs = ev.runs;
  S.detail = null;
  els.chatModel.textContent = ev.config?.model
    ? `${ev.config.model} · ${ev.config.base_url}`
    : "no model configured";
  R.renderChatFull(ev.current?.chat, els.chatLog);
  R.renderStatus(S.run, S.current, els);
  R.renderOverview(S.current, S.run, els, S.log);
  R.renderArtifact(S.current, S.run, els, approve);
  if (S.tab === "timeline") {
    R.renderTimeline(S.current?.calls, els, openDetail);
    if (pendingCall != null && S.current?.calls && S.current.calls.length > pendingCall) {
      openDetail(pendingCall);
      pendingCall = null;
    }
  }
  els.stats.textContent = statsText();
}

function onInit(ev) {
  if (S.current) {
    S.current.ws = ev.ws;
    if (ev.fresh) {
      S.current.calls = [];
      S.current.ledger = { plan: "", notes: "", tasks: [], artifact: "",
                           artifact_file: ev.artifact_file, kind: "" };
      S.current.tokens = { calls: 0, prompt_tokens: 0, completion_tokens: 0 };
    }
  }
  R.renderOverview(S.current, S.run, els, S.log);
  if (S.tab === "timeline" && S.detail == null) R.renderTimeline(S.current?.calls, els, openDetail);
}

function onCall(ev) {
  if (!S.current) return;
  S.current.calls.push({
    role: ev.role, request: ev.request, response: ev.response,
    prompt_tokens: ev.prompt_tokens, completion_tokens: ev.completion_tokens,
    finish_reason: ev.finish_reason, provider: ev.provider, attempts: ev.attempts,
  });
  if (S.current.tokens) {
    S.current.tokens.prompt_tokens = (S.current.tokens.prompt_tokens || 0) + (ev.prompt_tokens || 0);
    S.current.tokens.completion_tokens = (S.current.tokens.completion_tokens || 0) + (ev.completion_tokens || 0);
    S.current.tokens.calls = (S.current.tokens.calls || 0) + 1;
  }
  updateOverviewStats();
  if (S.tab === "timeline" && S.detail == null) R.renderTimeline(S.current.calls, els, openDetail);
}

function onLedger(ev) {
  if (!S.current) return;
  S.current.ledger = { ...S.current.ledger, ...ev };
  R.renderOverview(S.current, S.run, els, S.log);
  R.renderArtifact(S.current, S.run, els, approve);
}

function onVerify(ev) {
  toast(`sample tests: ${ev.passed}/${ev.total} passed`);
}

function onStatus(ev) {
  S.run = ev;
  R.renderStatus(S.run, S.current, els);
  els.stats.textContent = statsText();
}

function onChatToken(ev) {
  R.appendToken(els.chatLog, ev.id, ev.delta);
}

function onChatMessage(ev) {
  if (ev.role === "manager") R.finalizeStreaming(els.chatLog, ev.id, ev.text);
  else R.addMessage(els.chatLog, ev);
}

function onChatError(ev) {
  R.addMessage(els.chatLog, { id: ev.id, role: "error", text: `⚠ ${ev.message}` });
}

function onLog(ev) {
  S.log.push(ev.line);
  if (S.log.length > 200) S.log.splice(0, S.log.length - 200);
  const logEl = els.overview.querySelector("#ov-log");
  if (logEl) { logEl.textContent = S.log.join("\n"); logEl.scrollTop = logEl.scrollHeight; }
}

function onDone(ev) {
  S.run = { ...S.run, state: ev.stopped ? "stopped" : "done" };
  R.renderStatus(S.run, S.current, els);
  els.stats.textContent = statsText();
  toast(ev.has_artifact ? "run complete — artifact ready" : "run finished");
}

/* ---------------- derived views ---------------- */

function statsText() {
  const parts = [];
  if (S.current?.profile) parts.push(S.current.profile);
  if (S.run?.model) parts.push(S.run.model.replace(/^compat:/, ""));
  const calls = S.current?.calls?.length || 0;
  const toks = S.current?.tokens
    ? (S.current.tokens.prompt_tokens || 0) + (S.current.tokens.completion_tokens || 0) : 0;
  let s = parts.join(" · ");
  if (calls) s += ` · ${calls} call${calls > 1 ? "s" : ""} · ${fmtTokens(toks)} tok`;
  const st = S.run?.state;
  if ((st === "running" || st === "paused") && S.run?.started_at) {
    s += ` · ${fmtElapsed(Math.max(0, Date.now() / 1000 - S.run.started_at))}`;
  }
  return s;
}

function updateOverviewStats() {
  const c = els.overview.querySelector("#ov-calls");
  const t = els.overview.querySelector("#ov-toks");
  if (c && S.current) c.textContent = String(S.current.calls.length);
  if (t && S.current?.tokens) {
    t.textContent = fmtTokens((S.current.tokens.prompt_tokens || 0) + (S.current.tokens.completion_tokens || 0));
  }
}

function setTab(tab) {
  S.tab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  els.overview.hidden = tab !== "overview";
  els.timeline.hidden = tab !== "timeline";
  els.artifact.hidden = tab !== "artifact";
  els.files.hidden = tab !== "files";
  if (tab === "timeline") {
    S.detail = null;
    R.renderTimeline(S.current?.calls, els, openDetail);
  }
  if (tab === "artifact") R.renderArtifact(S.current, S.run, els, approve);
  if (tab === "files") loadFiles();
}

/* ---------------- workspace files ---------------- */

let filesOpen = false;   // true while a single file view is showing
async function loadFiles() {
  filesOpen = false;
  try {
    const r = await api("/api/files");
    R.renderFiles(r.files, els, openFile);
  } catch (e) {
    els.files.innerHTML = `<div class="empty" style="text-align:center;padding:44px 0">${esc(e.message)}</div>`;
  }
}
async function openFile(name) {
  try {
    const r = await api("/api/file?name=" + encodeURIComponent(name));
    filesOpen = true;
    R.renderFileView(r, els, loadFiles);
  } catch (e) {
    toast(e.message);
  }
}

function openDetail(i) {
  S.detail = i;
  R.renderCallDetail(S.current.calls[i], i, els, closeDetail);
}

function closeDetail() {
  S.detail = null;
  R.renderTimeline(S.current?.calls, els, openDetail);
}

/* ---------------- actions ---------------- */

async function doRun(fresh) {
  try { await api("/api/run", { fresh }); }
  catch (e) { toast(e.message); }
}

async function doNew() {
  const busy = S.run && (S.run.state === "running" || S.run.state === "paused");
  if (busy) return;
  if (S.current && (S.current.calls?.length || S.current.chat?.length) &&
      !confirm("Start a fresh task? The current conversation will be cleared.")) return;
  try {
    await api("/api/new", { method: "POST" });
    applyState(await api("/api/state"));
  } catch (e) { toast(e.message); }
}

async function openRuns() {
  els.modalRuns.hidden = false;
  try {
    const r = await api("/api/runs");
    R.renderRuns(r.runs, els, doOpenRun, doContinueRun);
  } catch (e) { toast(e.message); }
}

async function doOpenRun(slug) {
  els.modalRuns.hidden = true;
  try {
    const r = await api("/api/open", { slug });
    applyState(r.state);
  } catch (e) { toast(e.message); }
}

async function doContinueRun(slug) {
  els.modalRuns.hidden = true;
  try {
    const r = await api("/api/open", { slug });
    applyState(r.state);
    await api("/api/continue", {});
  } catch (e) { toast(e.message); }
}

async function approve() {
  try { await api("/api/approve", { method: "POST" }); toast("approved"); }
  catch (e) { toast(e.message); }
}

/* ---------------- settings ---------------- */

function openSettings() {
  const c = S.config || {};
  $("#cfg-base_url").value = c.base_url || "";
  $("#cfg-api_key").value = c.api_key || "";
  $("#cfg-model").value = c.model || "";
  $("#cfg-temperature").value = c.temperature ?? "";
  $("#cfg-max_tokens").value = c.max_tokens ?? "";
  $("#cfg-timeout").value = c.timeout ?? "";
  $("#cfg-max_iters").value = c.max_iters ?? "";
  $("#cfg-profile").value = c.profile || "general";
  $("#cfg-tests").value = c.tests || "";
  $("#cfg-search_provider").value = c.search_provider || "";
  $("#cfg-search_api_key").value = c.search_api_key || "";
  $("#cfg-search_url").value = c.search_url || "";
  $("#cfg-search_headers").value = (c.search_headers && Object.keys(c.search_headers).length)
    ? JSON.stringify(c.search_headers) : "";
  $("#cfg-search_mapping").value = (c.search_mapping && Object.keys(c.search_mapping).length)
    ? JSON.stringify(c.search_mapping) : "";
  $("#cfg-search_command").value = c.search_command || "";
  syncSearchFields();
  els.cfgModels.hidden = true;
  els.connResult.textContent = "";
  els.modalSettings.hidden = false;
}

function syncSearchFields() {
  const p = $("#cfg-search_provider").value;
  $("#field-search_api_key").hidden = p !== "brave";
  $("#field-search_url").hidden = p !== "http";
  $("#field-search_headers").hidden = p !== "http";
  $("#field-search_mapping").hidden = p !== "http";
  $("#field-search_command").hidden = p !== "command";
}

function parseJsonField(v) {
  v = (v || "").trim();
  if (!v) return {};
  try { return JSON.parse(v); } catch { return {}; }
}

function readCfgForm() {
  return {
    base_url: $("#cfg-base_url").value.trim(),
    api_key: $("#cfg-api_key").value,
    model: $("#cfg-model").value.trim(),
    temperature: $("#cfg-temperature").value === "" ? null : Number($("#cfg-temperature").value),
    max_tokens: Number($("#cfg-max_tokens").value || 131072),
    timeout: Number($("#cfg-timeout").value || 900),
    max_iters: Number($("#cfg-max_iters").value || 10),
    profile: $("#cfg-profile").value,
    tests: $("#cfg-tests").value,
    search_provider: $("#cfg-search_provider").value,
    search_api_key: $("#cfg-search_api_key").value,
    search_url: $("#cfg-search_url").value.trim(),
    search_headers: parseJsonField($("#cfg-search_headers").value),
    search_mapping: parseJsonField($("#cfg-search_mapping").value),
    search_command: $("#cfg-search_command").value.trim(),
  };
}

async function testConnection(populate) {
  const cfg = readCfgForm();
  els.connResult.textContent = "testing…";
  try {
    const r = await api("/api/config/test", cfg);
    if (r.ok) {
      if (populate) {
        els.cfgModels.innerHTML = (r.models || []).map((m) => `<option value="${m}">${m}</option>`).join("");
        els.cfgModels.hidden = !(r.models || []).length;
      }
      els.connResult.textContent = r.ok
        ? `✓ connected${(r.models || []).length ? ` · ${r.models.length} models` : ""}`
        : "✓ connected";
    } else {
      els.connResult.textContent = "✗ " + r.error;
    }
  } catch (e) {
    els.connResult.textContent = "✗ " + e.message;
  }
}

/* ---------------- toast ---------------- */

let toastTimer = null;
function toast(msg) {
  els.toast.textContent = msg;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 3500);
}

/* ---------------- wiring ---------------- */

els.btnRun.addEventListener("click", () => doRun(true));
els.btnPause.addEventListener("click", () => api("/api/pause", { method: "POST" }).catch((e) => toast(e.message)));
els.btnResume.addEventListener("click", () => api("/api/resume", { method: "POST" }).catch((e) => toast(e.message)));
els.btnStop.addEventListener("click", () => api("/api/stop", { method: "POST" }).catch((e) => toast(e.message)));
els.btnNew.addEventListener("click", doNew);
els.btnRuns.addEventListener("click", openRuns);
$("#btn-settings").addEventListener("click", openSettings);

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = els.chatText.value.trim();
  if (!text) return;
  els.chatText.value = "";
  try { await api("/api/chat", { message: text }); }
  catch (err) { toast(err.message); }
});
els.chatText.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); }
});

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cfg = readCfgForm();
  if (!cfg.model) { toast("set a model first — click “Fetch models” to list what your server offers"); return; }
  try {
    await api("/api/config", cfg);
    S.config = cfg;
    els.modalSettings.hidden = true;
    toast("settings saved");
    applyState(await api("/api/state"));
  } catch (err) { toast(err.message); }
});
$("#btn-fetch-models").addEventListener("click", () => testConnection(true));
$("#btn-test-conn").addEventListener("click", () => testConnection(false));
els.cfgModels.addEventListener("change", () => { $("#cfg-model").value = els.cfgModels.value; });
$("#cfg-search_provider").addEventListener("change", syncSearchFields);

document.querySelectorAll("[data-close]").forEach((el) =>
  el.addEventListener("click", () => { el.closest(".modal").hidden = true; }));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll(".modal:not([hidden])").forEach((m) => { m.hidden = true; });
});

/* ---------------- ticker ---------------- */

setInterval(() => {
  els.stats.textContent = statsText();
  const el = els.overview.querySelector("#ov-elapsed");
  if (el && S.run?.started_at && (S.run.state === "running" || S.run.state === "paused")) {
    el.textContent = fmtElapsed(Math.max(0, Date.now() / 1000 - S.run.started_at));
  }
}, 1000);

/* ---------------- init ---------------- */

connect();
setTab(wantedTab && ["overview", "timeline", "artifact", "files"].includes(wantedTab) ? wantedTab : "overview");
api("/api/state").then(applyState).catch(() => {});
