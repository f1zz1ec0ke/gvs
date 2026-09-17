"""The TUI: a live terminal view over the ledger-based orchestration loop.

The filesystem ledger is the source of truth, so the UI simply re-reads the
ledger files after each event. Steering (stop / pause / force-next / inject
note) talks to a :class:`gvs_tui.loop.Control` that the loop checks between
model calls.
"""

import os
import threading

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header, Footer, Static, Button, Input, Select,
    TabbedContent, TabPane, DataTable, Log, TextArea, Label,
)

from . import ledger
from .profiles import PROFILES, get_profile
from .verifier import run_python_tests
from .loop import Control, run_loop


class GvsApp(App):
    CSS = """
    Screen { align: left middle; }
    #task { height: 7; }
    #config-row, #controls, #steer { height: 3; }
    #tabs { height: 1fr; }
    #status { height: 1; dock: bottom; }
    #plan-view, #notes-view, #artifact-view { height: 1fr; }
    #ledger-pane { height: 1fr; }
    .btn { margin: 0 1; }
    """

    BINDINGS = [
        ("ctrl+s", "start", "start"),
        ("ctrl+x", "stop", "stop"),
        ("ctrl+p", "pause", "pause/resume"),
        ("ctrl+f", "focus_force", "force task"),
        ("ctrl+n", "focus_note", "inject note"),
        ("ctrl+q", "quit", "quit"),
    ]

    def __init__(self, task="", model="mock:gvs", profile="general",
                 iters=10, tests="", ws_root=None):
        super().__init__()
        self._init_task = task
        self._init_model = model
        self._init_profile = profile
        self._init_iters = iters
        self._init_tests = tests
        self._ws_root = ws_root

        self.control = Control()
        self.ws = None
        self.profile_obj = None
        self.status = None
        self.last_error = None
        self.running = False
        self._tokens = {"prompt": 0, "completion": 0, "calls": 0}

    # --- compose -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header("GVS-TUI  ·  ledger-based self-orchestration")
        with Vertical(id="main"):
            yield TextArea(self._init_task, id="task",
                           placeholder="Paste the task here (or load via the CLI).")
            with Horizontal(id="config-row"):
                yield Label("[dim] model [/]", markup=True)
                yield Input(self._init_model, id="model", placeholder="provider:model")
                yield Label("[dim] profile [/]", markup=True)
                yield Select([(n, n) for n in sorted(PROFILES)],
                             value=self._init_profile, id="profile")
                yield Label("[dim] iters [/]", markup=True)
                yield Input(str(self._init_iters), id="iters", placeholder="max iters")
                yield Label("[dim] tests [/]", markup=True)
                yield Input(self._init_tests, id="tests",
                            placeholder="optional: one 'input => output' per line (code profile)")
            with Horizontal(id="controls"):
                yield Button("Start", id="start", variant="success")
                yield Button("Stop", id="stop", variant="error")
                yield Button("Pause", id="pause", variant="warning")
                yield Label(id="run-state", content="idle")
            with Horizontal(id="steer"):
                yield Label("[dim] force-next [/]", markup=True)
                yield Input(id="force-task", placeholder="override the manager's next task")
                yield Button("Force", id="force-btn", variant="primary")
                yield Label("[dim] inject-note [/]", markup=True)
                yield Input(id="note", placeholder="append a note to the shared ledger")
                yield Button("Note", id="note-btn", variant="primary")
            with TabbedContent(id="tabs"):
                with TabPane("Ledger", id="tab-ledger"):
                    with Vertical(id="ledger-pane"):
                        yield Static("# plan.md", id="plan-head", markup=True)
                        yield Static("", id="plan-view")
                        yield Static("# notes.md", id="notes-head", markup=True)
                        yield Static("", id="notes-view")
                with TabPane("Tasks", id="tab-tasks"):
                    yield DataTable(id="tasks")
                with TabPane("Transcript", id="tab-transcript"):
                    yield Log(id="transcript", highlight=False)
                with TabPane("Artifact", id="tab-artifact"):
                    yield Static("", id="artifact-view")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("id", "status", "description")
        table.cursor_type = "rows"
        self._set_status("idle")

    # --- helpers -------------------------------------------------------------

    def _set_status(self, text):
        self.query_one("#status").update(text)

    def _set_run_state(self, text):
        self.query_one("#run-state").update(text)

    def _transcript(self, line):
        self.query_one("#transcript", Log).write(line)

    def _refresh_ledger(self):
        if not self.ws:
            return
        self.query_one("#plan-view").update(
            ledger.read(self.ws, "plan.md") or "(no plan yet)")
        self.query_one("#notes-view").update(
            ledger.read(self.ws, "notes.md") or "(no notes yet)")
        art = ledger.read(self.ws, self.profile_obj.artifact_file) \
            if self.profile_obj else ""
        self.query_one("#artifact-view").update(art or "(no artifact yet)")
        table = self.query_one("#tasks", DataTable)
        table.clear()
        for t in ledger.load_tasks(self.ws):
            table.add_row(str(t.get("id", "")), t.get("status", ""), t.get("desc", ""))

    def _start(self):
        if self.running:
            return
        task = self.query_one("#task", TextArea).text.strip()
        if not task:
            self._transcript("[error] the task is empty -- paste a task first")
            return
        model = self.query_one("#model", Input).value.strip() or "mock:gvs"
        profile_name = self.query_one("#profile", Select).value
        try:
            iters = int(self.query_one("#iters", Input).value.strip() or "10")
        except ValueError:
            iters = 10
        tests_raw = self.query_one("#tests", Input).value.strip()

        profile = get_profile(profile_name)
        self.profile_obj = profile
        self._tokens = {"prompt": 0, "completion": 0, "calls": 0}
        self.control = Control()

        verifier, tests = None, None
        if profile.kind == "code" and tests_raw:
            tests = _parse_tests(tests_raw)
            if tests:
                verifier = run_python_tests

        self.running = True
        self._set_run_state("running")
        self._transcript(f"\n== start: model={model} profile={profile.name} "
                         f"iters<={iters} verifier={'yes' if verifier else 'no'} ==")
        t = threading.Thread(
            target=self._loop_worker,
            args=(task, profile, model, iters, verifier, tests),
            daemon=True)
        t.start()

    def _loop_worker(self, task, profile, model, iters, verifier, tests):
        def events(etype, payload):
            self.call_from_thread(self._on_event, etype, payload)
        try:
            status = run_loop(
                task, profile, model,
                ws_root=self._ws_root,
                max_iters=iters,
                verifier=verifier, tests=tests,
                control=self.control,
                events=events,
                log=lambda *a: None,
            )
            self.call_from_thread(self._on_done, status)
        except Exception as e:  # noqa: BLE001 - surfaced in the UI
            self.call_from_thread(self._on_error, e)

    # --- event handlers (main thread) ----------------------------------------

    def _on_event(self, etype, payload):
        if etype == "init":
            self.ws = payload["ws"]
            self.profile_obj = get_profile(payload["profile"])
            self._transcript(f"[init] workspace: {self.ws}")
        elif etype == "call":
            p = payload.get("prompt_tokens") or 0
            c = payload.get("completion_tokens") or 0
            self._tokens["calls"] += 1
            self._tokens["prompt"] += p
            self._tokens["completion"] += c
            self._transcript(
                f"  [{payload.get('role')}] "
                f"tok {p}+{c} finish={payload.get('finish_reason')} "
                f"provider={payload.get('provider')}")
            self._set_status(self._status_text())
        elif etype == "log":
            self._transcript(payload["line"])
        elif etype == "verify":
            self._transcript(f"  [verify] {payload.get('passed')}/{payload.get('total')} passed")
        self._refresh_ledger()

    def _status_text(self):
        return (f"calls {self._tokens['calls']}  ·  tokens "
                f"{self._tokens['prompt']}+{self._tokens['completion']}  ·  "
                f"ws {os.path.basename(self.ws) if self.ws else '-'}")

    def _on_done(self, status):
        self.running = False
        self.status = status
        self._set_run_state("done" if not status.get("stopped") else "stopped")
        self._transcript(
            f"\n== done: iters={status['iters']} calls={status['n_calls']} "
            f"artifact={'yes' if status['has_artifact'] else 'NO'} ==")
        self._set_status(self._status_text() + "  ·  finished")
        self._refresh_ledger()

    def _on_error(self, exc):
        self.running = False
        self.last_error = f"{type(exc).__name__}: {exc}"
        self._set_run_state("error")
        self._transcript(f"\n[error] {type(exc).__name__}: {exc}")
        self._set_status("error")

    # --- actions -------------------------------------------------------------

    def action_start(self):
        self._start()

    def action_stop(self):
        if self.running:
            self.control.stop()
            self._transcript("[stop] requested")

    def action_pause(self):
        if self.running:
            paused = self.control.toggle_pause()
            self.query_one("#pause", Button).label = "Resume" if paused else "Pause"
            self._set_run_state("paused" if paused else "running")

    def action_focus_force(self):
        self.query_one("#force-task", Input).focus()

    def action_focus_note(self):
        self.query_one("#note", Input).focus()

    # --- button events -------------------------------------------------------

    def on_button_pressed(self, event) -> None:
        if event.button.id == "start":
            self._start()
        elif event.button.id == "stop":
            self.action_stop()
        elif event.button.id == "pause":
            self.action_pause()
        elif event.button.id == "force-btn":
            text = self.query_one("#force-task", Input).value.strip()
            if text and self.running:
                self.control.force_next(text)
                self.query_one("#force-task", Input).value = ""
                self._transcript(f"[user] forced next task: {text}")
        elif event.button.id == "note-btn":
            text = self.query_one("#note", Input).value.strip()
            if text and self.ws:
                ledger.append(self.ws, "notes.md", f"\n## user note\n{text}\n")
                self.query_one("#note", Input).value = ""
                self._transcript(f"[user] injected note: {text[:60]}")
                self._refresh_ledger()


def _parse_tests(raw):
    """Parse 'input => output' lines into sample-test dicts."""
    tests = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=>" in line:
            inp, out = line.split("=>", 1)
            tests.append({"input": inp.strip(), "output": out.strip()})
    return tests
