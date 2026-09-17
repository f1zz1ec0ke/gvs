// Frontend rendering verification: run the real md.js + render.js logic in Node
// with a minimal DOM shim, and assert the generated HTML is correct.
//
// Run:  node --experimental-default-type=module tests/test_frontend.mjs

import * as md from "../gvs_tui/web/static/js/md.js";
import * as R from "../gvs_tui/web/static/js/render.js";
import { api, fmtTokens, fmtElapsed, fmtAgo } from "../gvs_tui/web/static/js/api.js";

let failures = 0;
function check(name, cond) {
  if (cond) console.log("  ok  " + name);
  else { console.log("  FAIL " + name); failures++; }
}

// ---- minimal DOM shim ----
function fakeEl() {
  const el = {
    innerHTML: "", textContent: "", className: "", hidden: false, disabled: false,
    value: "", title: "", dataset: {}, style: {},
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {}, requestSubmit() {},
    querySelector: () => fakeEl(),
    querySelectorAll: () => [],
    closest: () => null,
  };
  return el;
}
function els() {
  return {
    pill: fakeEl(), label: fakeEl(), stats: fakeEl(),
    btnRun: fakeEl(), btnPause: fakeEl(), btnResume: fakeEl(), btnStop: fakeEl(),
    btnNew: fakeEl(), btnRuns: fakeEl(),
    chatLog: fakeEl(), chatText: fakeEl(), chatModel: fakeEl(),
    overview: fakeEl(), timeline: fakeEl(), artifact: fakeEl(),
    runsList: fakeEl(), conn: fakeEl(), toast: fakeEl(),
    modalSettings: fakeEl(), modalRuns: fakeEl(), connResult: fakeEl(), cfgModels: fakeEl(),
  };
}

console.log("md.js");
check("esc escapes < & >", md.esc("<a>&\"b\"") === "&lt;a&gt;&amp;&quot;b&quot;");
const mdHtml = md.md("### Plan\n- step one\n- **bold** and `code`\n```python\nprint(1)\n```\nDone.");
check("md renders heading", mdHtml.includes("<h4>Plan</h4>"));
check("md renders list items", mdHtml.includes("<li>step one</li>") && mdHtml.includes("<li><strong>bold</strong>"));
check("md renders inline code", mdHtml.includes('<code class="inline">code</code>'));
check("md renders fenced python highlighted", mdHtml.includes("tok-num") && mdHtml.includes("print"));
check("md empty -> dash", md.md("").includes("—"));
const secs = md.sections("### STATUS\ndone\n### NEXT\nfix it\n### TASKS\n- [todo] a");
check("sections extracts 3", secs.length === 3);
check("sections names", secs[0].name === "STATUS" && secs[1].name === "NEXT" && secs[2].name === "TASKS");
check("sections bodies", secs[0].body === "done" && secs[1].body === "fix it");

console.log("api.js");
check("fmtTokens 12345 -> 12k", fmtTokens(12345) === "12k");
check("fmtTokens 950 -> 950", fmtTokens(950) === "950");
check("fmtElapsed 75 -> 1m 15s", fmtElapsed(75) === "1m 15s");
check("fmtElapsed 3700 -> 1h 1m", fmtElapsed(3700) === "1h 1m");
check("fmtAgo just now", fmtAgo(Date.now() / 1000) === "just now");

console.log("api.js — HTTP method selection (regression: POST vs GET)");
{
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, json: async () => ({ ok: true }) };
  };
  try {
    await api("/api/state");                 // GET, no body
    await api("/api/config", { model: "x" }); // raw body -> POST
    await api("/api/new", { method: "POST" }); // explicit POST, no body
    await api("/api/open", { slug: "abc" });  // raw body -> POST
    await api("/api/config/test", { base_url: "u", model: "m" }); // raw body -> POST
  } finally {
    globalThis.fetch = realFetch;
  }
  check("state -> GET, no body", calls[0].opts.method === "GET" && calls[0].opts.body === undefined);
  check("config -> POST + body", calls[1].opts.method === "POST" && calls[1].opts.body === JSON.stringify({ model: "x" }));
  check("new -> POST, no body", calls[2].opts.method === "POST" && calls[2].opts.body === undefined);
  check("open -> POST + body", calls[3].opts.method === "POST" && calls[3].opts.body === JSON.stringify({ slug: "abc" }));
  check("config/test -> POST + body", calls[4].opts.method === "POST" && calls[4].opts.body === JSON.stringify({ base_url: "u", model: "m" }));
}

console.log("render.js — status");
{
  const e = els();
  R.renderStatus({ state: "running", model: "compat:gvs-mock" }, { profile: "code", task: "t", calls: [1], tokens: { prompt_tokens: 100, completion_tokens: 50 } }, e);
  check("pill class running", e.pill.className === "pill running");
  check("label running", e.label.textContent === "running");
  check("run disabled while running", e.btnRun.disabled === true);
  check("pause visible while running", e.btnPause.hidden === false);
  R.renderStatus({ state: "idle" }, null, e);
  check("pill idle", e.pill.className === "pill idle");
  check("run disabled with no task", e.btnRun.disabled === true);
}

console.log("render.js — overview");
{
  const e = els();
  const current = {
    task: "Read an int, print its square.",
    profile: "code",
    ledger: { plan: "### Plan\n- do x", notes: "note", tasks: [{ desc: "plan", status: "done" }, { desc: "implement", status: "in_progress" }, { desc: "verify", status: "pending" }], artifact: "print(1)", artifact_file: "solution.py", kind: "code" },
    calls: [1, 2],
    tokens: { prompt_tokens: 100, completion_tokens: 50 },
  };
  R.renderOverview(current, { state: "done", started_at: Date.now() / 1000 - 42 }, e, ["log line"]);
  const h = e.overview.innerHTML;
  check("overview has task", h.includes("Read an int, print its square."));
  check("overview has plan heading", h.includes("<h4>Plan</h4>"));
  check("overview task done icon", h.includes("✓"));
  check("overview task in-progress icon", h.includes("●"));
  check("overview task pending icon", h.includes("○"));
  check("overview calls stat", h.includes("id=\"ov-calls\">2"));
  check("overview tokens stat", h.includes("id=\"ov-toks\">150"));
  check("overview elapsed stat", h.includes("id=\"ov-elapsed\""));
  check("overview activity log box", h.includes('id="ov-log"'));
}

console.log("render.js — timeline");
{
  const e = els();
  const calls = [
    { role: "primary_plan", prompt_tokens: 10, completion_tokens: 20, finish_reason: "stop", response: "### PLAN\nx" },
    { role: "worker:1", prompt_tokens: 30, completion_tokens: 40, finish_reason: "stop", response: "print(1)" },
  ];
  let selected = null;
  R.renderTimeline(calls, e, (i) => { selected = i; });
  const h = e.timeline.innerHTML;
  check("timeline has 2 items", (h.match(/tl-item/g) || []).length >= 2);
  check("timeline role label Plan", h.includes("Plan"));
  check("timeline role label Worker", h.includes("Worker"));
  check("timeline token meta", h.includes("10→20 tok"));
  check("timeline snippet", h.includes("print(1)"));
}

console.log("render.js — call detail");
{
  const e = els();
  const call = { role: "worker:1", prompt_tokens: 30, completion_tokens: 40, finish_reason: "stop", provider: "compat", attempts: 1,
    request: [{ role: "system", content: "You are a worker" }, { role: "user", content: "Do the task" }],
    response: "### NOTES\nstuff\n### ARTIFACT\n```python\nprint(1)\n```\n### STATUS\ndone" };
  R.renderCallDetail(call, 1, e, () => {});
  const h = e.timeline.innerHTML;
  check("detail breadcrumb", h.includes("crumb-back"));
  check("detail call number", h.includes("Call 2"));
  check("detail role chip", h.includes("Worker"));
  check("detail prompt system", h.includes("You are a worker"));
  check("detail prompt user", h.includes("Do the task"));
  check("detail raw response", h.includes("### NOTES"));
  check("detail parsed section ARTIFACT", h.includes("ARTIFACT"));
  check("detail copy button", h.includes("copy-resp"));
}

console.log("render.js — call detail (tool call)");
{
  const e = els();
  const toolResp = '```tool\n{"name": "write_file", "args": {"path": "result.md", "content": "# Brief"}}\n```';
  const call = { role: "worker:1", prompt_tokens: 30, completion_tokens: 40, finish_reason: "stop", provider: "compat", attempts: 1,
    request: [{ role: "system", content: "You are a worker" }, { role: "user", content: "Do the task" }],
    response: toolResp };
  R.renderCallDetail(call, 1, e, () => {});
  const h = e.timeline.innerHTML;
  check("tool chip shown", h.includes("chip tool") && h.includes("write_file"));
  check("tool section shown", h.includes("Tool call") && h.includes("tool-card"));
  check("tool args summarised", h.includes("path=result.md") && h.includes("content=&lt;7 chars&gt;"));
}

console.log("render.js — artifact");
{
  const e = els();
  const current = { ledger: { artifact: "print(1)", kind: "code", artifact_file: "solution.py" } };
  R.renderArtifact(current, { state: "done" }, e, () => {});
  const h = e.artifact.innerHTML;
  check("artifact approve button", h.includes("approve-btn"));
  check("artifact highlighted code", h.includes("tok-num") || h.includes("print"));
  // no artifact
  const e2 = els();
  R.renderArtifact({ ledger: { artifact: "", kind: "code" } }, { state: "running" }, e2, () => {});
  check("artifact empty message", e2.artifact.innerHTML.includes("No artifact yet"));
}

console.log("render.js — runs list");
{
  const e = els();
  const runs = [{ slug: "abc", task: "Read an int", profile: "code", model: "gvs-mock", n_calls: 5, has_artifact: true, updated: Date.now() / 1000 - 3600 }];
  R.renderRuns(runs, e, () => {}, () => {});
  const h = e.runsList.innerHTML;
  check("runs row task", h.includes("Read an int"));
  check("runs row meta", h.includes("5 calls"));
  check("runs artifact badge", h.includes("badge-art"));
  check("runs open button", h.includes('data-act="open"'));
  check("runs continue button", h.includes('data-act="continue"'));
  const e2 = els();
  R.renderRuns([], e2, () => {}, () => {});
  check("runs empty message", e2.runsList.innerHTML.includes("No saved runs"));
}

console.log("");
if (failures) { console.log(failures + " FRONTEND CHECK(S) FAILED"); process.exit(1); }
console.log("ALL FRONTEND RENDERING CHECKS PASSED");
