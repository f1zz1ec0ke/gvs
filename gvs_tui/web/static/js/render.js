// DOM rendering for the gvs web GUI.

import { esc, md, sections, highlightPy } from "./md.js";
import { fmtTokens, fmtElapsed, fmtAgo } from "./api.js";

export const ROLES = {
  primary_plan:   { label: "Plan",     color: "#a78bfa" },
  ideation:       { label: "Ideate",   color: "#22d3ee" },
  primary_manage: { label: "Manage",   color: "#fbbf24" },
  worker:         { label: "Worker",   color: "#4ade80" },
  cutoff_summary: { label: "Cutoff",   color: "#fb7185" },
  finalize:       { label: "Finalize", color: "#60a5fa" },
};

export function roleMeta(role) {
  if (!role) return { label: "call", color: "#8b95a7" };
  if (ROLES[role]) return ROLES[role];
  // Worker calls are recorded as "worker:<id>"; map them to the worker role.
  const base = role.split(":")[0];
  if (ROLES[base]) return ROLES[base];
  return { label: role, color: "#8b95a7" };
}

function h(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function firstLine(s) {
  if (!s) return "(empty)";
  const l = String(s).split("\n").map((x) => x.trim()).find(Boolean) || "(empty)";
  return l.length > 92 ? l.slice(0, 92) + "…" : l;
}

/* ---------------- status ---------------- */

export function renderStatus(run, current, els) {
  const state = (run && run.state) || "idle";
  els.pill.className = "pill " + state;
  els.label.textContent = state;

  const busy = state === "running" || state === "paused";
  els.btnRun.disabled = busy || !current || !current.task;
  els.btnRun.textContent = busy ? "Running…" : "▶ Run";
  els.btnPause.hidden = state !== "running";
  els.btnResume.hidden = state !== "paused";
  els.btnStop.hidden = !busy;
  els.btnNew.disabled = busy;
  els.btnRuns.disabled = busy;
}

/* ---------------- chat ---------------- */

function hideEmpty(chatLogEl) {
  const e = chatLogEl.querySelector(".chat-empty");
  if (e) e.remove();
}

function emptyStateHtml() {
  return '<div class="chat-empty"><div class="chat-empty-mark">🦍</div>' +
    "<p>Describe a task to the manager —<br>it will plan, delegate to workers, and you can watch it all live.</p></div>";
}

function scrollChat(el, force) {
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
  if (force || nearBottom) el.scrollTop = el.scrollHeight;
}

export function renderChatFull(chat, chatLogEl) {
  chatLogEl.innerHTML = "";
  if (!chat || !chat.length) {
    chatLogEl.innerHTML = emptyStateHtml();
    return;
  }
  for (const m of chat) addMessage(chatLogEl, m);
  chatLogEl.scrollTop = chatLogEl.scrollHeight;
}

function thinkingHtml() {
  return `<div class="thinking">` +
    `<div class="thinking-toggle" role="button" tabindex="0">` +
    `<span class="thinking-dot"></span>` +
    `<span class="thinking-label">thinking…</span>` +
    `<span class="thinking-caret">▸</span></div>` +
    `<div class="thinking-body"></div></div>`;
}

function attachThinkingToggle(t) {
  if (!t || t.dataset.bound) return;
  t.dataset.bound = "1";
  const tog = t.querySelector(".thinking-toggle");
  const toggle = () => t.classList.toggle("open");
  tog.addEventListener("click", toggle);
  tog.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
}

function setThinking(el, reasoning, active) {
  let t = el.querySelector(".thinking");
  if (!t) {
    t = h(thinkingHtml());
    el.querySelector(".body").before(t);
  }
  if (reasoning != null) t.querySelector(".thinking-body").textContent = reasoning;
  t.querySelector(".thinking-label").textContent = active ? "thinking…" : "thinking";
  t.classList.toggle("active", !!active);
  attachThinkingToggle(t);
  return t;
}

export function addMessage(chatLogEl, m) {
  hideEmpty(chatLogEl);
  const cls = m.role === "user" ? "user" : m.role === "manager" ? "manager" : "system";
  const el = h(
    `<div class="msg ${cls}" ${m.id != null ? `data-id="${m.id}"` : ""}>` +
    (m.role !== "system" ? `<div class="who">${esc(m.role)}</div>` : "") +
    `<div class="body">${esc(m.text)}</div></div>`
  );
  if (m.reasoning) setThinking(el, m.reasoning, false);
  chatLogEl.appendChild(el);
  scrollChat(chatLogEl, true);
  return el;
}

export function startStreaming(chatLogEl, id) {
  hideEmpty(chatLogEl);
  const el = h(
    `<div class="msg manager" data-id="${id}">` +
    `<div class="who">manager</div>` +
    `<div class="body"><span class="txt"></span><span class="caret"></span></div></div>`
  );
  chatLogEl.appendChild(el);
  scrollChat(chatLogEl, true);
  return el;
}

export function appendToken(chatLogEl, id, delta) {
  let el = chatLogEl.querySelector(`.msg[data-id="${id}"]`);
  if (!el) el = startStreaming(chatLogEl, id);
  const txt = el.querySelector(".txt");
  txt.textContent += delta;
  // content has started -> the thinking phase is over (stop the animation)
  const t = el.querySelector(".thinking");
  if (t && txt.textContent) t.classList.remove("active");
  scrollChat(chatLogEl, false);
}

export function appendThinking(chatLogEl, id, delta) {
  let el = chatLogEl.querySelector(`.msg[data-id="${id}"]`);
  if (!el) el = startStreaming(chatLogEl, id);
  const t = setThinking(el, null, true);  // active = still thinking
  t.querySelector(".thinking-body").textContent += delta;
  scrollChat(chatLogEl, false);
}

export function finalizeStreaming(chatLogEl, id, text, reasoning) {
  const el = chatLogEl.querySelector(`.msg[data-id="${id}"]`);
  if (!el) { addMessage(chatLogEl, { id, role: "manager", text, reasoning }); return; }
  const body = el.querySelector(".body");
  body.innerHTML = text
    ? esc(text)
    : '<span class="dim">(no reply -- it was still thinking; expand "thinking" above to see what it was considering)</span>';
  if (reasoning) setThinking(el, reasoning, false);
  scrollChat(chatLogEl, true);
}

/* ---------------- overview ---------------- */

function taskHtml(t) {
  const st = t.status === "done" ? "done" : t.status === "in_progress" ? "in_progress" : "pending";
  const ic = st === "done" ? "✓" : st === "in_progress" ? "●" : "○";
  return `<div class="task ${st}"><span class="ic">${ic}</span><span class="desc">${esc(t.desc)}</span></div>`;
}

export function renderOverview(current, run, els, logLines) {
  const cur = current || {};
  const led = cur.ledger || {};
  const tasks = led.tasks || [];
  const calls = (cur.calls || []).length;
  const toks = cur.tokens ? (cur.tokens.prompt_tokens || 0) + (cur.tokens.completion_tokens || 0) : 0;
  const elapsed = run && run.started_at ? Math.max(0, Date.now() / 1000 - run.started_at) : 0;

  let html = `<div class="stats-row">
    <div class="stat"><div class="k">Elapsed</div><div class="v" id="ov-elapsed">${fmtElapsed(elapsed)}</div></div>
    <div class="stat"><div class="k">Calls</div><div class="v" id="ov-calls">${calls}</div></div>
    <div class="stat"><div class="k">Tokens</div><div class="v" id="ov-toks">${fmtTokens(toks)}</div></div>
    <div class="stat"><div class="k">Profile</div><div class="v">${esc(cur.profile || "—")}</div></div>
  </div>`;

  if (cur.task) {
    html += `<div class="card"><div class="card-head">Task</div>
      <div class="card-body"><p style="margin:0;font-size:13px;line-height:1.5">${esc(cur.task)}</p></div></div>`;
  }
  html += `<div class="card"><div class="card-head">Plan</div>
    <div class="card-body md">${md(led.plan)}</div></div>`;
  html += `<div class="card"><div class="card-head">Tasks</div>
    <div class="card-body">${tasks.length ? tasks.map(taskHtml).join("") : '<div class="empty">no tasks yet</div>'}</div></div>`;
  html += `<div class="card"><div class="card-head">Notes</div>
    <div class="card-body md" style="max-height:260px;overflow:auto">${md(led.notes)}</div></div>`;
  html += `<div class="card"><div class="card-head">Activity</div>
    <div class="card-body"><div class="log-box" id="ov-log"></div></div></div>`;

  els.overview.innerHTML = html;
  const logEl = els.overview.querySelector("#ov-log");
  if (logEl) logEl.textContent = (logLines || []).slice(-200).join("\n") || "(quiet)";
}

/* ---------------- timeline ---------------- */

export function renderTimeline(calls, els, onSelect) {
  const tl = els.timeline;
  if (!calls || !calls.length) {
    tl.innerHTML = '<div class="empty" style="text-align:center;padding:44px 0">No model calls yet — start a run and watch every call land here.</div>';
    return;
  }
  let html = '<div class="tl">';
  calls.forEach((c, i) => {
    const rm = roleMeta(c.role);
    html += `<div class="tl-item" data-i="${i}" style="--role:${rm.color}">
      <div class="idx">${i + 1}</div>
      <div class="main">
        <div class="row1">
          <span class="tl-role">${rm.label}</span>
          <span class="meta">${fmtTokens(c.prompt_tokens || 0)}→${fmtTokens(c.completion_tokens || 0)} tok · ${esc(c.finish_reason || "?")}</span>
        </div>
        <div class="snippet">${esc(firstLine(c.response))}</div>
      </div>
    </div>`;
  });
  html += "</div>";
  tl.innerHTML = html;
  tl.querySelectorAll(".tl-item").forEach((el) =>
    el.addEventListener("click", () => onSelect(Number(el.dataset.i))));
}

/* ---------------- call detail ---------------- */

/* Parse a ```tool fenced JSON block (or TOOLCALL: line) out of a model response.
   Mirrors the backend gvs_tui.tools.parse_tool_block. */
function parseToolCall(text) {
  if (!text) return null;
  let m = text.match(/```tool[ \t]*\n([\s\S]*?)```/);
  if (!m) {
    const m2 = text.match(/^\s*TOOLCALL:\s*(\{.*\})\s*$/m);
    if (!m2) return null;
    m = m2;
  }
  try {
    const obj = JSON.parse(m[1].trim());
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
    const name = obj.name;
    if (typeof name !== "string" || !name.trim()) return null;
    const args = obj.args && typeof obj.args === "object" && !Array.isArray(obj.args) ? obj.args : {};
    return { name: name.trim(), args };
  } catch { return null; }
}

function toolArgSummary(args) {
  const parts = [];
  for (const [k, v] of Object.entries(args || {})) {
    if (k === "content" && typeof v === "string") parts.push(`content=<${v.length} chars>`);
    else if (typeof v === "string") parts.push(`${k}=${v.length > 40 ? v.slice(0, 37) + "…" : v}`);
    else parts.push(`${k}=${JSON.stringify(v)}`);
  }
  return parts.join(", ");
}

export function renderCallDetail(call, index, els, onBack) {
  const tl = els.timeline;
  const rm = roleMeta(call.role);
  const secs = sections(call.response);
  const toolCall = parseToolCall(call.response);

  let html = `<button class="crumb" id="crumb-back">← Timeline</button>`;
  html += `<div class="detail-head" style="--role:${rm.color}">
    <h3>Call ${index + 1}</h3>
    <span class="chip role">${rm.label}</span>
    ${toolCall ? `<span class="chip tool" title="This call is a tool invocation">🔧 ${esc(toolCall.name)}</span>` : ""}
    <span class="chip">${fmtTokens(call.prompt_tokens || 0)}→${fmtTokens(call.completion_tokens || 0)} tok</span>
    <span class="chip">${esc(call.finish_reason || "?")}</span>
    ${call.provider ? `<span class="chip">${esc(call.provider)}</span>` : ""}
    ${call.attempts ? `<span class="chip">${call.attempts} attempt${call.attempts > 1 ? "s" : ""}</span>` : ""}
  </div>`;

  if (toolCall) {
    html += `<div class="detail-section tool-section">
      <div class="label">Tool call</div>
      <div class="tool-card">
        <div class="tool-name">🔧 ${esc(toolCall.name)}<span class="tool-args">${esc(toolArgSummary(toolCall.args))}</span></div>
        <pre class="code"><code>${esc(JSON.stringify(toolCall, null, 2))}</code></pre>
      </div></div>`;
  }

  html += `<div class="detail-section"><div class="label">Prompt</div>`;
  for (const m of (call.request || [])) {
    html += `<div class="msg-block"><div class="mrole">${esc(m.role)}</div>
      <pre class="code"><code>${esc(m.content)}</code></pre></div>`;
  }
  html += `</div>`;

  html += `<div class="detail-section">
    <div class="label">Raw response <button class="copy-btn" id="copy-resp">copy</button></div>
    <pre class="code"><code>${esc(call.response || "(empty)")}</code></pre></div>`;

  if (secs.length) {
    html += `<div class="detail-section"><div class="label">Parsed sections</div>`;
    for (const s of secs) {
      html += `<div class="card" style="margin-bottom:10px">
        <div class="card-head">${esc(s.name)}</div>
        <div class="card-body md">${md(s.body)}</div></div>`;
    }
    html += `</div>`;
  }

  tl.innerHTML = html;
  tl.querySelector("#crumb-back").addEventListener("click", onBack);
  const cp = tl.querySelector("#copy-resp");
  if (cp) cp.addEventListener("click", () => {
    try { navigator.clipboard.writeText(call.response || ""); } catch { /* ignore */ }
    cp.textContent = "copied";
    setTimeout(() => { cp.textContent = "copy"; }, 1200);
  });
}

/* ---------------- artifact ---------------- */

function hasHtml(files) {
  return (files || []).some((f) => /\.html?$/i.test(f.name));
}

function artifactActionsHtml(finished, files, onApprove, onPreview) {
  if (!finished || !files.length) return "";
  return `<div class="artifact-actions">
    <button class="btn primary" id="approve-btn">✓ Approve deliverable</button>
    ${hasHtml(files) ? '<button class="btn" id="preview-btn">▶ Preview</button>' : ""}
    <span class="artifact-hint">Not happy? Tell the manager what to change in the chat, then Run again.</span>
  </div>`;
}

function wireArtifactActions(a, onApprove, onPreview) {
  const ab = a.querySelector("#approve-btn");
  if (ab) ab.addEventListener("click", onApprove);
  const pb = a.querySelector("#preview-btn");
  if (pb) pb.addEventListener("click", onPreview);
}

export function renderArtifact(current, run, els, onApprove, onOpenFile, onPreview) {
  const a = els.artifact;
  const cur = current || {};
  const led = cur.ledger || {};
  const files = led.out_files || [];
  const state = (run && run.state) || "idle";
  const finished = state === "done" || state === "stopped";

  let html = artifactActionsHtml(finished, files, onApprove, onPreview);
  if (!files.length) {
    html += '<div class="empty" style="text-align:center;padding:44px 0">No deliverable yet — files the workers write to <code>out/</code> will appear here.</div>';
    a.innerHTML = html;
    wireArtifactActions(a, onApprove, onPreview);
    return;
  }
  const rows = files.map((f) => `
    <div class="file-row" data-name="out/${esc(f.name)}" title="click to open">
      <span class="file-name">${esc(f.name)}</span>
      <span class="file-size">${fmtSize(f.size)}</span>
    </div>`).join("");
  html += `<div class="files-head">Deliverable — out/ <span class="files-count">${files.length}</span></div>
    <div class="file-list">${rows}</div>`;
  a.innerHTML = html;
  a.querySelectorAll(".file-row").forEach((el) =>
    el.addEventListener("click", () => onOpenFile(el.dataset.name)));
  wireArtifactActions(a, onApprove, onPreview);
}

export function renderArtifactFileView(file, els, onBack) {
  const box = els.artifact;
  const { name, content, kind, truncated } = file;
  let body;
  if (kind === "code") {
    body = `<pre class="code" style="max-height:72vh;overflow:auto"><code>${highlightPy(content)}</code></pre>`;
  } else if (kind === "md") {
    body = `<div class="md" style="max-height:72vh;overflow:auto">${md(content)}</div>`;
  } else {
    body = `<pre class="code plain" style="max-height:72vh;overflow:auto;white-space:pre-wrap">${esc(content)}</pre>`;
  }
  box.innerHTML = `
    <div class="file-view-head">
      <button class="btn" id="artifact-file-back">← All deliverable files</button>
      <span class="file-view-name">${esc(name)}</span>
      ${truncated ? '<span class="file-trunc">truncated</span>' : ""}
    </div>
    ${body}`;
  const back = box.querySelector("#artifact-file-back");
  if (back) back.addEventListener("click", onBack);
}

/* ---------------- workspace files ---------------- */

function fmtSize(n) {
  if (n == null) return "";
  n = Number(n);
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

export function renderFiles(files, els, onOpen) {
  const box = els.files;
  const list = files || [];
  if (!list.length) {
    box.innerHTML = '<div class="empty" style="text-align:center;padding:44px 0">No workspace open — start a run (or open a saved run) to see its files.</div>';
    return;
  }
  const rows = list.map((f) => `
    <div class="file-row" data-name="${esc(f.name)}" title="click to open">
      <span class="file-name">${esc(f.name)}</span>
      <span class="file-size">${fmtSize(f.size)}</span>
    </div>`).join("");
  box.innerHTML = `
    <div class="files-head">Workspace files <span class="files-count">${list.length}</span></div>
    <div class="file-list">${rows}</div>`;
  box.querySelectorAll(".file-row").forEach((el) =>
    el.addEventListener("click", () => onOpen(el.dataset.name)));
}

export function renderFileView(file, els, onBack) {
  const box = els.files;
  const { name, content, kind, truncated } = file;
  let body;
  if (kind === "code") {
    body = `<pre class="code" style="max-height:72vh;overflow:auto"><code>${highlightPy(content)}</code></pre>`;
  } else if (kind === "md") {
    body = `<div class="md" style="max-height:72vh;overflow:auto">${md(content)}</div>`;
  } else {
    body = `<pre class="code plain" style="max-height:72vh;overflow:auto;white-space:pre-wrap">${esc(content)}</pre>`;
  }
  box.innerHTML = `
    <div class="file-view-head">
      <button class="btn" id="file-back">← All files</button>
      <span class="file-view-name">${esc(name)}</span>
      ${truncated ? '<span class="file-trunc">truncated</span>' : ""}
    </div>
    ${body}`;
  const back = box.querySelector("#file-back");
  if (back) back.addEventListener("click", onBack);
}

/* ---------------- runs list ---------------- */

export function renderRuns(runs, els, onOpen, onContinue) {
  const list = els.runsList;
  if (!runs || !runs.length) {
    list.innerHTML = '<div class="empty" style="text-align:center;padding:22px 0">No saved runs yet — finished runs are stored under <code>runs/ws</code>.</div>';
    return;
  }
  list.innerHTML = runs.map((r) => `
    <div class="run-row" data-slug="${esc(r.slug)}">
      <div class="info">
        <div class="task-line">${esc(r.task || r.slug)}</div>
        <div class="sub">${esc(r.profile || "")} · ${esc(r.model || "")} · ${r.n_calls} calls · ${fmtAgo(r.updated)}</div>
      </div>
      ${r.has_artifact ? '<span class="badge-art">artifact</span>' : ""}
      <div class="btns">
        <button class="btn mini" data-act="open">Open</button>
        <button class="btn mini" data-act="continue">Continue</button>
      </div>
    </div>`).join("");
  list.querySelectorAll(".run-row").forEach((row) => {
    const slug = row.dataset.slug;
    row.querySelector('[data-act="open"]').addEventListener("click", () => onOpen(slug));
    row.querySelector('[data-act="continue"]').addEventListener("click", () => onContinue(slug));
  });
}
