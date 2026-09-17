"""FastAPI backend for the gvs-tui web GUI.

One process serves the single-page frontend and a WebSocket that streams live
events (loop calls, ledger changes, streaming chat tokens, run status). A single
run is active at a time. The OpenAI-compatible endpoint is config-driven
(``compat:`` provider) so any base_url + key + model works (e.g. local LM Studio).
"""

import os
import json
import time
import asyncio
import dataclasses
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import ledger
import gvs_tui.chat as chatmod
from ..chat import set_compat_endpoint, compat_chat_stream, fetch_models
from ..profiles import PROFILES, get_profile
from ..verifier import run_python_tests
from ..loop import Control, run_loop

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
WS_ROOT = os.environ.get("GVS_WS_ROOT", os.path.join(PROJECT_ROOT, "runs", "ws"))


# --- config -----------------------------------------------------------------

class Config(BaseModel):
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = ""
    model: str = ""
    temperature: float | None = None   # None -> use the profile's per-role temps
    max_tokens: int = 131072
    timeout: int = 900
    profile: str = "general"
    tests: str = ""                    # one "input => output" per line (code profile)
    max_iters: int = 10


def load_config() -> Config:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return Config(**json.load(f))
        except Exception:
            pass
    return Config()


def save_config(cfg: Config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg.model_dump(), f, indent=2)


def apply_config(cfg: Config):
    """Point the compat provider + global knobs at the user's config."""
    set_compat_endpoint(cfg.base_url, cfg.api_key)
    chatmod.CLOUD_MAX_TOKENS = int(cfg.max_tokens)
    chatmod.CLOUD_TIMEOUT = int(cfg.timeout)


def parse_tests(raw):
    tests = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if "=>" in line:
            inp, out = line.split("=>", 1)
            tests.append({"input": inp.strip(), "output": out.strip()})
    return tests


def safe_profile(name):
    """get_profile that falls back to 'general' instead of SystemExit."""
    return get_profile(name) if name in PROFILES else get_profile("general")


# --- live event broadcaster -------------------------------------------------

class Broadcaster:
    """Fan-out of events to connected WebSocket clients (thread-safe)."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []
        self._loop = None

    def attach(self, q):
        self._queues.append(q)

    def detach(self, q):
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def broadcast(self, event: dict):
        if not self._queues or self._loop is None:
            return
        for q in list(self._queues):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass


# --- manager chat hub -------------------------------------------------------

class ChatHub:
    """Owns chat message ids and posts to the shared ledger + broadcaster."""

    def __init__(self, broadcaster, get_ws):
        self.broadcaster = broadcaster
        self.get_ws = get_ws
        self._id = 0
        self._lock = threading.Lock()

    def _new_id(self):
        with self._lock:
            self._id += 1
            return self._id

    def post(self, role, text):
        ws = self.get_ws()
        if ws:
            ledger.append_chat(ws, role, text)
        mid = self._new_id()
        self.broadcaster.broadcast(
            {"type": "chat_message", "id": mid, "role": role, "text": text,
             "t": time.time()})
        return mid

    def post_user(self, text):
        return self.post("user", text)

    def stream_manager(self, messages, model_name):
        ws = self.get_ws()
        mid = self._new_id()

        def work():
            meta = {}
            parts = []
            try:
                for delta in compat_chat_stream(model_name, messages,
                                                temperature=0.3, meta=meta):
                    parts.append(delta)
                    self.broadcaster.broadcast(
                        {"type": "chat_token", "id": mid, "delta": delta})
                full = "".join(parts)
                if ws:
                    ledger.append_chat(ws, "manager", full)
                self.broadcaster.broadcast(
                    {"type": "chat_message", "id": mid, "role": "manager",
                     "text": full, "t": time.time()})
            except Exception as e:  # noqa: BLE001 - surfaced to the client
                self.broadcaster.broadcast(
                    {"type": "chat_error", "id": mid,
                     "message": f"{type(e).__name__}: {e}"})

        threading.Thread(target=work, daemon=True).start()
        return mid


def manager_messages(ws, text, profile):
    task = ledger.read(ws, "task.md")
    plan = ledger.read(ws, "plan.md")
    notes = ledger.read(ws, "notes.md")
    chat = ledger.recent_chat(ws, 12)
    domain = profile.domain if profile else "the task at hand"
    system = (
        f"You are the PRIMARY orchestrator (manager) of a small team of worker "
        f"subagents, all expert at {domain}, coordinating through a shared "
        "filesystem ledger. The user is talking to you directly. Be concise and "
        "practical: acknowledge the task, explain your plan, answer questions, "
        "and give clear direction. You do NOT write the final artifact yourself -- "
        "worker subagents do. Keep replies to a few sentences unless the user asks "
        "for detail."
    )
    user = (
        f"TASK:\n{task}\n\n"
        f"CURRENT PLAN:\n{plan or '(none yet)'}\n\n"
        f"NOTES:\n{notes or '(none yet)'}\n\n"
        f"RECENT CHAT:\n{chat or '(none)'}\n\n"
        f"USER JUST SAID:\n{text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --- run manager -------------------------------------------------------------

class RunManager:
    """Owns the single active run (one at a time)."""

    def __init__(self, broadcaster, state):
        self.broadcaster = broadcaster
        self.state = state
        self.control = None
        self.thread = None
        self.state_name = "idle"     # idle|running|paused|done|stopped|error
        self.ws = None
        self.profile = None
        self.model = None
        self.started_at = None
        self.status = None
        self._n_calls = 0
        self._lock = threading.Lock()

    def is_busy(self):
        return self.state_name in ("running", "paused")

    def start(self, task_text, profile, model, max_iters, verifier, tests, fresh=True):
        with self._lock:
            if self.is_busy():
                raise RuntimeError("a run is already active")
            self.control = Control()
            self.state_name = "running"
            self.ws = None
            self.profile = profile
            self.model = model
            self.started_at = time.time()
            self.status = None
            self._n_calls = 0
        t = threading.Thread(
            target=self._worker,
            args=(task_text, profile, model, max_iters, verifier, tests, fresh),
            daemon=True)
        self.thread = t
        t.start()

    def _worker(self, task_text, profile, model, max_iters, verifier, tests, fresh):
        try:
            status = run_loop(
                task_text, profile, model,
                ws_root=WS_ROOT, max_iters=max_iters,
                verifier=verifier, tests=tests,
                control=self.control,
                events=self._on_loop_event,
                log=lambda *a: None, fresh=fresh)
            self.status = status
            self.state_name = "stopped" if status.get("stopped") else "done"
        except Exception as e:  # noqa: BLE001 - surfaced to the client
            self.state_name = "error"
            self.broadcaster.broadcast(
                {"type": "error", "message": f"{type(e).__name__}: {e}"})
        self.broadcaster.broadcast({"type": "status", **self.status_payload()})

    def _on_loop_event(self, etype, payload):
        if etype == "init":
            self.ws = payload["ws"]
            self.state.current_ws = self.ws
            self.broadcaster.broadcast({"type": "init", **payload})
            self.state.hub.post("manager",
                                "Starting the run -- I'll lay out a plan and get workers on it.")
        elif etype == "call":
            self._n_calls += 1
            self.broadcaster.broadcast({"type": "call", "index": self._n_calls - 1,
                                        **payload})
        elif etype == "plan":
            self._broadcast_ledger()
            self.state.hub.post("manager", f"Plan ready -- {payload['n_tasks']} tasks.")
        elif etype == "ideation":
            self.state.hub.post("manager",
                                f"Ideation done -- {payload['n_proposals']} distinct approaches on the table.")
        elif etype == "manage":
            self._broadcast_ledger()
            self.state.hub.post("manager", self._manage_message(payload))
        elif etype == "worker":
            self._broadcast_ledger()
            self.state.hub.post("manager", self._worker_message(payload))
        elif etype == "verify":
            self.broadcaster.broadcast({"type": "verify", **payload})
            self.state.hub.post("manager",
                                f"Sample tests: {payload.get('passed')}/{payload.get('total')} passed.")
        elif etype == "log":
            self.broadcaster.broadcast({"type": "log", "line": payload["line"]})
        elif etype == "done":
            self.broadcaster.broadcast({"type": "done", **payload})
            self.state.hub.post("manager", self._done_message(payload))

    def _broadcast_ledger(self):
        self.broadcaster.broadcast({"type": "ledger", **self.ledger_payload()})

    def ledger_payload(self):
        if not self.ws or not self.profile:
            return {}
        return {
            "plan": ledger.read(self.ws, "plan.md"),
            "notes": ledger.read(self.ws, "notes.md"),
            "tasks": ledger.load_tasks(self.ws),
            "artifact": ledger.read(self.ws, self.profile.artifact_file),
            "artifact_file": self.profile.artifact_file,
            "kind": self.profile.kind,
        }

    def _manage_message(self, p):
        update = (p.get("update") or "").strip()
        if p["status"] == "done":
            return update or "Marking the task done -- the artifact is ready."
        msg = update
        if p.get("next"):
            msg = (msg + " " if msg else "") + f"Next: {p['next']}"
        return msg or f"Continuing -- next: {p.get('next', '')}"

    def _worker_message(self, p):
        extra = " and wrote the artifact." if p.get("wrote") else ""
        return f"Worker finished task {p['task_id']} ({p['status']}){extra}."

    def _done_message(self, p):
        if p.get("has_artifact"):
            return "Done -- the artifact is ready for your review."
        return "Finished, but no artifact was produced. You can send it back to me."

    def status_payload(self):
        tokens = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        if self.ws:
            tokens = ledger.usage_totals(self.ws)
        return {
            "state": self.state_name,
            "ws": self.ws,
            "model": self.model,
            "profile": self.profile.name if self.profile else None,
            "started_at": self.started_at,
            "elapsed": (time.time() - self.started_at) if self.started_at else 0.0,
            "tokens": tokens,
            "n_calls": self._n_calls,
            "status": self.status,
        }

    def stop(self):
        if self.control:
            self.control.stop()
            if self.state_name == "running":
                self.state_name = "stopped"
            self.broadcaster.broadcast({"type": "status", **self.status_payload()})

    def pause(self):
        if self.control and self.state_name == "running":
            self.control.toggle_pause()
            self.state_name = "paused"
            self.broadcaster.broadcast({"type": "status", **self.status_payload()})

    def resume(self):
        if self.control and self.state_name == "paused":
            self.control.toggle_pause()
            self.state_name = "running"
            self.broadcaster.broadcast({"type": "status", **self.status_payload()})


# --- server state ------------------------------------------------------------

class ServerState:
    def __init__(self):
        self.cfg = load_config()
        self.current_ws = None
        self.current_task = None
        self.current_profile = None
        self.broadcaster = Broadcaster()
        self.hub = ChatHub(self.broadcaster, lambda: self.current_ws)
        self.runs = RunManager(self.broadcaster, self)


def ensure_workspace(st, task):
    if not st.current_ws:
        st.current_task = task
        profile = safe_profile(st.cfg.profile)
        st.current_ws = os.path.join(WS_ROOT, ledger.slug(task))
        st.current_profile = profile
        ledger.init_workspace(st.current_ws, profile.artifact_file, task)
        st.broadcaster.broadcast({"type": "init", "ws": st.current_ws,
                                  "model": f"compat:{st.cfg.model}",
                                  "profile": profile.name,
                                  "artifact_file": profile.artifact_file,
                                  "fresh": True})
    return st.current_ws


def state_payload(st):
    ws = st.current_ws
    profile = st.current_profile
    calls, chat, ledger_data = [], [], {}
    if ws:
        recs = [r for r in ledger.read_transcript(ws) if not r.get("_meta")]
        for i, r in enumerate(recs):
            calls.append({
                "index": i, "role": r.get("role"),
                "request": r.get("request"), "response": r.get("response"),
                "prompt_tokens": r.get("prompt_tokens"),
                "completion_tokens": r.get("completion_tokens"),
                "finish_reason": r.get("finish_reason"),
                "provider": r.get("provider"), "t": r.get("t"),
            })
        chat = ledger.read_chat(ws)
        if profile:
            ledger_data = {
                "plan": ledger.read(ws, "plan.md"),
                "notes": ledger.read(ws, "notes.md"),
                "tasks": ledger.load_tasks(ws),
                "artifact": ledger.read(ws, profile.artifact_file),
                "artifact_file": profile.artifact_file,
                "kind": profile.kind,
            }
    tokens = (ledger.usage_totals(ws) if ws
              else {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    return {
        "config": st.cfg.model_dump(),
        "run": st.runs.status_payload(),
        "current": {
            "ws": ws, "task": st.current_task,
            "profile": profile.name if profile else None,
            "ledger": ledger_data, "calls": calls, "chat": chat, "tokens": tokens,
        },
        "runs": ledger.list_runs(WS_ROOT),
    }


# --- request models ----------------------------------------------------------

class ChatIn(BaseModel):
    message: str


class RunIn(BaseModel):
    task: str | None = None
    fresh: bool = True


class OpenIn(BaseModel):
    slug: str


# --- app factory -------------------------------------------------------------

def create_app(state: ServerState | None = None) -> FastAPI:
    app = FastAPI(title="gvs-tui web")
    st = state or ServerState()
    app.state.gvs = st

    @app.on_event("startup")
    async def _startup():
        st.broadcaster._loop = asyncio.get_running_loop()
        apply_config(st.cfg)

    # ---- static frontend ----
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def _index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    # ---- state / config ----
    @app.get("/api/state")
    def _state():
        return state_payload(st)

    @app.get("/api/config")
    def _get_config():
        return st.cfg.model_dump()

    @app.post("/api/config")
    def _save_config(cfg: Config):
        st.cfg = cfg
        save_config(cfg)
        apply_config(cfg)
        return {"ok": True}

    @app.post("/api/config/test")
    def _test_config(cfg: Config):
        try:
            models = fetch_models(cfg.base_url, cfg.api_key)
            return {"ok": True, "models": models}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}

    # ---- runs list ----
    @app.get("/api/runs")
    def _runs():
        return {"runs": ledger.list_runs(WS_ROOT)}

    # ---- workspace file browser ----
    _CODE_EXT = (".py", ".js", ".ts", ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".rb", ".java")
    _MAX_FILE_DISPLAY = 200_000  # bytes; larger files are truncated in the browser

    @app.get("/api/files")
    def _files():
        if not st.current_ws or not os.path.isdir(st.current_ws):
            return {"files": []}
        files = []
        for name in sorted(os.listdir(st.current_ws)):
            p = os.path.join(st.current_ws, name)
            if os.path.isfile(p):
                s = os.stat(p)
                files.append({"name": name, "size": s.st_size, "mtime": s.st_mtime})
        return {"files": files}

    @app.get("/api/file")
    def _file(name: str):
        if not st.current_ws or not os.path.isdir(st.current_ws):
            raise HTTPException(404, "no workspace open")
        # reject path traversal / directory names
        if (not name or name in (".", "..") or ".." in name
                or "/" in name or "\\" in name or name.startswith("~")):
            raise HTTPException(400, "invalid file name")
        p = os.path.join(st.current_ws, name)
        if not os.path.isfile(p):
            raise HTTPException(404, "file not found")
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise HTTPException(500, f"read error: {e}")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"name": name, "content": "(binary file — not shown)",
                    "kind": "binary", "truncated": False, "size": len(raw)}
        truncated = len(content) > _MAX_FILE_DISPLAY
        if truncated:
            content = content[:_MAX_FILE_DISPLAY] + "\n\n… [truncated — file is larger than 200 KB]"
        lower = name.lower()
        if lower.endswith(_CODE_EXT):
            kind = "code"
        elif lower.endswith((".md", ".markdown", ".txt")):
            kind = "md"
        elif lower.endswith((".json", ".jsonl")):
            kind = "json"
        else:
            kind = "text"
        return {"name": name, "content": content, "kind": kind,
                "truncated": truncated, "size": len(raw)}

    # ---- chat ----
    @app.post("/api/chat")
    def _chat(body: ChatIn):
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "empty message")
        apply_config(st.cfg)
        ensure_workspace(st, text)
        st.hub.post_user(text)
        msgs = manager_messages(st.current_ws, text, st.current_profile)
        if not st.cfg.model:
            st.hub.post("manager",
                        "No model configured yet -- set the model in Settings, then I can get to work.")
            return {"ok": True}
        st.hub.stream_manager(msgs, st.cfg.model)
        return {"ok": True}

    # ---- run control ----
    @app.post("/api/run")
    def _run(body: RunIn):
        if st.runs.is_busy():
            raise HTTPException(409, "a run is already active")
        task = (body.task or st.current_task or "").strip()
        if not task:
            raise HTTPException(400, "no task yet -- send a task in the chat first")
        apply_config(st.cfg)
        ensure_workspace(st, task)
        profile = safe_profile(st.cfg.profile)
        if st.cfg.temperature is not None:
            profile = dataclasses.replace(
                profile, temps={k: st.cfg.temperature for k in profile.temps})
        st.current_profile = profile
        tests = parse_tests(st.cfg.tests)
        verifier = run_python_tests if (profile.kind == "code" and tests) else None
        if not st.cfg.model:
            raise HTTPException(400, "no model configured -- set it in Settings")
        st.runs.start(task, profile, f"compat:{st.cfg.model}",
                      st.cfg.max_iters, verifier, tests, fresh=body.fresh)
        return {"ok": True}

    @app.post("/api/stop")
    def _stop():
        st.runs.stop()
        return {"ok": True}

    @app.post("/api/pause")
    def _pause():
        st.runs.pause()
        return {"ok": True}

    @app.post("/api/resume")
    def _resume():
        st.runs.resume()
        return {"ok": True}

    # ---- workspace management ----
    @app.post("/api/new")
    def _new():
        if st.runs.is_busy():
            raise HTTPException(409, "a run is already active")
        st.current_ws = None
        st.current_task = None
        st.current_profile = None
        return {"ok": True}

    @app.post("/api/open")
    def _open(body: OpenIn):
        if st.runs.is_busy():
            raise HTTPException(409, "a run is already active")
        ws = os.path.join(WS_ROOT, body.slug)
        if not os.path.isdir(ws):
            raise HTTPException(404, "run not found")
        st.current_ws = ws
        st.current_task = ledger.read(ws, "task.md")
        metas = [r for r in ledger.read_transcript(ws) if r.get("_meta")]
        prof_name = metas[0].get("profile") if metas else st.cfg.profile
        st.current_profile = safe_profile(prof_name)
        return {"ok": True, "state": state_payload(st)}

    @app.post("/api/continue")
    def _continue(body: RunIn):
        if st.runs.is_busy():
            raise HTTPException(409, "a run is already active")
        if not st.current_ws:
            raise HTTPException(400, "no run to continue")
        apply_config(st.cfg)
        task = st.current_task or ledger.read(st.current_ws, "task.md")
        profile = st.current_profile or safe_profile(st.cfg.profile)
        tests = parse_tests(st.cfg.tests)
        verifier = run_python_tests if (profile.kind == "code" and tests) else None
        if not st.cfg.model:
            raise HTTPException(400, "no model configured -- set it in Settings")
        st.runs.start(task, profile, f"compat:{st.cfg.model}",
                      st.cfg.max_iters, verifier, tests, fresh=False)
        return {"ok": True}

    # ---- artifact verdict ----
    @app.post("/api/approve")
    def _approve():
        if not st.current_ws:
            raise HTTPException(400, "no run")
        ledger.write(st.current_ws, "approved.json",
                     json.dumps({"approved": True, "t": time.time()}))
        st.hub.post("manager", "Approved -- thanks. The run is complete.")
        return {"ok": True}

    # ---- websocket ----
    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await websocket.accept()
        q = asyncio.Queue()
        st.broadcaster.attach(q)
        try:
            await websocket.send_json({"type": "state", **state_payload(st)})
            while True:
                event = await q.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            st.broadcaster.detach(q)

    return app


# --- entry point -------------------------------------------------------------

# Module-level app so `uvicorn gvs_tui.web.server:app` works out of the box.
app = create_app()


def main(host="127.0.0.1", port=8321):
    import uvicorn
    print(f"gvs-tui web: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
