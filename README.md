# gvs-tui

A **general-purpose harness** (terminal TUI **and** a live web GUI) for
*ledger-based zero-shot self-orchestration* — the paradigm from the
[GVS5H / "Gorilla vs 5 Humans"](https://github.com/slee-persis/GVS5H) paper, lifted out of
benchmarking and made into a tool you point at *any* task.

## The paradigm

One model is invoked in **fresh contexts** as several roles. The roles never talk to
each other directly; they coordinate only through a **shared filesystem ledger**:

| File | Role |
|---|---|
| `task.md` | the task text (immutable) |
| `plan.md` | the manager's overarching plan |
| `notes.md` | shared notes, **rewritten** by each worker (last writer wins) |
| `tasks.json` | the manager's live task list |
| `solution.py` / `answer.md` / `report.md` | the current artifact (profile-defined) |
| `transcript.jsonl` | one line per model call (request, response, usage) |

The loop:

1. **primary_plan** — the manager writes `plan.md` + an initial task list.
2. **ideation** — a first worker proposes several *distinct* approaches (prose only).
3. **primary_manage** — the manager curates the task list and picks the **one** next task,
   or declares `done`.
4. **worker** — a fresh worker does that one task, rewrites `notes.md`, updates the
   artifact, and proposes next steps.
5. **(verifier)** — optional ground-truth check of the artifact (e.g. run sample tests),
   fed back to the manager as a pass/fail verdict.
6. Repeat 3–5 until the manager says `done` (and an artifact exists), the iteration cap is
   hit, or no progress is made. A **finalize** worker makes one last attempt if needed.

The key insight from the paper: decomposition + persistent context (the ledger) lets fresh
instances of a *single* model approach frontier coding accuracy — no training, no big model.

## What's general-purpose here

GVS5H hard-coded the loop around LiveCodeBench with `code`/`math` specs. This project
generalizes it:

- **Pluggable domain profiles** (`gvs_tui/profiles.py`) — a profile supplies the domain
  name, the worker's solver prompt, the artifact file + kind, and an answer marker.
  Built-ins: `code`, `math`, `research`, `general`. Add your own with one `Profile(...)`.
- **Optional verifier hooks** (`gvs_tui/verifier.py`) — any `verifier(ws, profile, tests)`
  callable that returns a pass/fail dict. The `code` profile ships a run-the-tests verifier.
- **Live TUI** (`gvs_tui/app.py`) — paste a task, pick model/profile, watch the ledger,
  task list, transcript, and token usage update in real time, and steer the run.
- **Live web GUI** (`gvs_tui/web/`) — a build-free single-page app (vanilla JS/CSS) served by
  the same FastAPI process. Chat with the manager, hit **Run**, and watch the whole run live:
  a streaming chat, a workbench (plan / task checklist / notes / artifact), a **Files** browser
  for everything in the workspace, and a **timeline** of every model call you can zoom into
  (full prompt, raw response, parsed sections, and each `🔧` tool step). Runs are saved and can
  be reopened or continued.
- **Mock provider** (`mock:` in `gvs_tui/chat.py`) — a deterministic fake model so the whole
  loop runs and is verifiable with **no API key**.

## Shared workspace & file tools

The ledger is not just read-only text pasted into prompts. Every run owns a **real
workspace directory** (`runs/ws/<slug>`) that the agents can **read and write**. Workers get
a minimal, **sandboxed file toolset** — `ls`, `read_file`, `write_file`, `append_file` —
confined to that directory (no shell, no network, no code execution), driven by a
model-agnostic **JSON-in-text tool protocol** (a `tool`-tagged fenced JSON block), so it
works on any OpenAI-compatible endpoint.

- A **worker** uses the tools to *actually create* its deliverable (written to the profile's
  artifact file, e.g. `result.md`) plus any supporting files (research notes, drafts,
  subdirectories). It no longer has to cram everything into one response, and it can't
  "describe" a file it didn't write — the file is real and verifiable.
- The **manager** gets **read-only** tools (`ls`, `read_file`) plus a live file listing, so it
  can inspect what workers actually produced before deciding the next step.
- Each tool call is a recorded model call: the **Timeline** shows every tool step (a green
  `🔧` chip with the tool name and args), and the **Files** tab lets you browse everything that
  exists in the workspace.
- Safety: every path is resolved and checked to stay under the workspace root, writes are
  size-capped, and there's a cap on tool calls per role turn. The **verifier** remains the only
  code-execution path (harness-controlled, for ground-truth tests).

## Install

```bash
cd gvs-tui
python -m venv .venv
.venv\Scripts\pip install -e ".[web]"            # Windows
# .venv/bin/pip install -e ".[web]"             # macOS/Linux
```

The base package needs `textual>=8`; the `[web]` extra adds `fastapi`, `uvicorn[standard]`,
and `httpx` for the browser GUI. Provider API keys for the TUI/CLI come from the environment
(see `.env.sample`); the **web GUI takes its endpoint/key/model from the in-app Settings
dialog** (persisted to `config.json`) and works with any OpenAI-compatible server
(LM Studio, vLLM, Ollama's OpenAI endpoint, LiteLLM, real OpenAI). `mock:` needs no key.

## Run

```bash
# Demo: mock model, built-in coding task + sample tests. No key needed.
.venv\Scripts\python -m gvs_tui.cli --mock

# Real model, TUI
.venv\Scripts\python -m gvs_tui.cli --model openai:gpt-5 --profile code --task "..."

# Task from a file, headless (no TUI), logs to stdout
.venv\Scripts\python -m gvs_tui.cli --task-file task.md --profile research --headless

# Code profile with your own sample tests (one 'input => output' per line)
.venv\Scripts\python -m gvs_tui.cli --model ollama:qwen3:35b --profile code \
    --tests "3 => 9" --task "Read an int, print its square"
```

Or install the entry point (`pip install -e .`) and call `gvs-tui ...`.

## Web GUI

```bash
# Start the web server (serves the SPA + WebSocket on one port)
.venv\Scripts\python -m gvs_tui.cli --web                 # http://127.0.0.1:8321
.venv\Scripts\python -m gvs_tui.cli --web --port 9000     # custom port
# or, once installed:  gvs-tui --web   /   gvs-tui-web
```

Then open the printed URL. The workflow:

1. **Settings** (⚙) — point it at your OpenAI-compatible endpoint (base URL, key, model).
   *Fetch models* lists what the server offers; *Test connection* verifies it.
2. **Chat** the task to the manager. It acknowledges and explains its plan (streamed live).
3. **Run** — the orchestration loop starts. Watch:
   - the **chat** fill with the manager's progress (plan ready, worker finished, tests, done);
   - the **Overview** tab — plan, live task checklist, notes, and an activity log;
   - the **Timeline** tab — every model call as it lands; click any call to **zoom in** and read
     the full prompt, the raw response, the parsed `###` sections, and any tool steps;
    - the **Files** tab — browse every file in the workspace (including what workers wrote);
   - the **Artifact** tab — the current artifact (syntax-highlighted for code), with an
     **Approve** button once the run finishes.
4. **Steer** anytime — send the manager a message (it reads the ledger + your chat), or
   **Pause** / **Resume** / **Stop** the run.
5. **Runs** — every run is saved under `runs/ws/<slug>`; reopen one to browse its full
   timeline, or **Continue** an incomplete one.

One run is active at a time. The server is a single local process; no build step, no frontend
toolchain — the SPA is plain ES modules served from `gvs_tui/web/static`.

### TUI keys

| Key | Action |
|---|---|
| `ctrl+s` | start the run |
| `ctrl+x` | stop |
| `ctrl+p` | pause / resume |
| `ctrl+f` | focus the **force-next** input (override the manager's next task) |
| `ctrl+n` | focus the **inject-note** input (append a note to the shared ledger) |
| `ctrl+q` | quit |

Steering is faithful to the paradigm: *force-next* injects a task the loop will pick up on
its next iteration; *inject-note* appends to `notes.md`, which every subsequent fresh worker
and the manager will read from the ledger.

## Tests

```bash
.venv\Scripts\python tests\test_loop.py        # headless loop, mock provider (3 profiles)
.venv\Scripts\python tests\test_tools.py       # sandboxed file tools: confinement, caps, parsing
.venv\Scripts\python tests\test_tool_loop.py   # end-to-end tool loop (worker writes a file via a tool call)
.venv\Scripts\python tests\test_tui.py         # TUI mounts + live views update (Textual pilot)
.venv\Scripts\python tests\test_steering.py    # stop / force-next / pause drive the loop
.venv\Scripts\python tests\test_web.py         # web backend: REST + WebSocket + file browser (mock compat)
.venv\Scripts\python tests\test_compat_http.py # compat provider vs a local OpenAI server
.venv\Scripts\python tests\live_e2e.py         # full live E2E: web server + real HTTP loop
node tests\test_frontend.mjs                   # frontend rendering (md, render, api method selection)
```

## Project layout

```
gvs_tui/
  chat.py       provider dispatch (ollama/openai/groq/dashscope/anthropic/openrouter/claude
                + config-driven `compat:` OpenAI-compatible endpoint + mock)
  ledger.py     workspace files, transcript, section/task parsing, manager chat, saved runs
  profiles.py   pluggable domain profiles
  verifier.py   optional ground-truth verifier hooks
  tools.py      sandboxed file tools (ls/read/write/append) + JSON tool-call protocol
  loop.py       the manager/worker orchestration loop + tool loop + Control (steering)
  app.py        the Textual TUI
  cli.py        command-line entry point (--web launches the GUI)
  web/
    server.py   FastAPI app: config, run manager, chat hub, WebSocket broadcaster, REST
    static/     build-free SPA (index.html, style.css, js/{app,render,md,api}.js)
tests/          loop, TUI, steering, web backend, compat HTTP, and live E2E tests
```

## License

MIT. The orchestration loop and provider layer are adapted from GVS5H
(MIT, © 2026 Persis Capital Inc.); see that repo's `NOTICE.md` for the upstream licensing.
