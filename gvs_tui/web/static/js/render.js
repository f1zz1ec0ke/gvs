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

export function addMessage(chatLogEl, m) {
  hideEmpty(chatLogEl);
  const cls = m.role === "user" ? "user" : m.role === "manager" ? "manager" : "system";
  const el = h(
    `<div class="msg ${cls}" ${m.id != null ? `data-id="${m.id}"` : ""}>` +
    (m.role !== "system" ? `<div class="who">${esc(m.role)}</div>` : "") +
    `<div class="body">${esc(m.text)}</div></div>`
  );
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
  scrollChat(chatLogEl, false);
}

export function finalizeStreaming(chatLogEl, id, text) {
  const el = chatLogEl.querySelector(`.msg[data-id="${id}"]`);
  if (!el) { addMessage(chatLogEl, { id, role: "manager", text }); return; }
  el.querySelector(".body").innerHTML = esc(text);
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

export function renderCallDetail(call, index, els, onBack) {
  const tl = els.timeline;
  const rm = roleMeta(call.role);
  const secs = sections(call.response);

  let html = `<button class="crumb" id="crumb-back">← Timeline</button>`;
  html += `<div class="detail-head" style="--role:${rm.color}">
    <h3>Call ${index + 1}</h3>
    <span class="chip role">${rm.label}</span>
    <span class="chip">${fmtTokens(call.prompt_tokens || 0)}→${fmtTokens(call.completion_tokens || 0)} tok</span>
    <span class="chip">${esc(call.finish_reason || "?")}</span>
    ${call.provider ? `<span class="chip">${esc(call.provider)}</span>` : ""}
    ${call.attempts ? `<span class="chip">${call.attempts} attempt${call.attempts > 1 ? "s" : ""}</span>` : ""}
  </div>`;

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

export function renderArtifact(current, run, els, onApprove) {
  const a = els.artifact;
  const cur = current || {};
  const led = cur.ledger || {};
  const art = led.artifact || "";
  const kind = led.kind || "code";
  const state = (run && run.state) || "idle";
  const finished = state === "done" || state === "stopped";

  let html = "";
  if (finished && art) {
    html += `<div class="artifact-actions">
      <button class="btn primary" id="approve-btn">✓ Approve artifact</button>
      <span class="artifact-hint">Not happy? Tell the manager what to change in the chat, then Run again.</span>
    </div>`;
  }
  if (!art) {
    html += '<div class="empty" style="text-align:center;padding:44px 0">No artifact yet — it will appear here as workers produce it.</div>';
    a.innerHTML = html;
    return;
  }
  if (kind === "code") {
    html += `<pre class="code" id="artifact-code"><code>${highlightPy(art)}</code></pre>`;
  } else {
    html += `<div class="md" style="max-height:70vh;overflow:auto">${md(art)}</div>`;
  }
  a.innerHTML = html;
  const ab = a.querySelector("#approve-btn");
  if (ab) ab.addEventListener("click", onApprove);
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
