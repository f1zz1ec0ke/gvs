"""Backend tests: REST + WebSocket, with the compat provider patched to the
deterministic mock so no network is needed.

Run:  python tests/test_web.py
"""

import os
import sys
import time
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import gvs_tui.chat as chatmod
from gvs_tui.chat import mock_chat
import gvs_tui.web.server as server
from gvs_tui.web.server import create_app, ServerState
from fastapi.testclient import TestClient

# isolate workspace + config under a temp dir
TMP = tempfile.mkdtemp(prefix="gvs_web_test_")
server.WS_ROOT = os.path.join(TMP, "runs", "ws")
server.CONFIG_PATH = os.path.join(TMP, "config.json")


def fake_compat_chat(model, messages, temperature=0.2, meta=None):
    return mock_chat(model, messages, temperature, meta)


def fake_compat_chat_stream(model, messages, temperature=0.2, meta=None):
    full = mock_chat(model, messages, temperature, meta)
    for ch in full:
        yield ("content", ch)


# patch both the module global (used by the loop's dispatch) and server's own
# binding (used by the chat hub's streaming).
chatmod.compat_chat = fake_compat_chat
chatmod.compat_chat_stream = fake_compat_chat_stream
server.compat_chat_stream = fake_compat_chat_stream


def wait_for(client, pred, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get("/api/state").json()
        if pred(s):
            return s
        time.sleep(0.1)
    raise TimeoutError("condition not met within timeout")


def test_rest_flow():
    st = ServerState()
    st.cfg.model = "gvs"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9\n1 => 1"
    st.cfg.max_iters = 6
    app = create_app(st)
    with TestClient(app) as client:
        # config round-trip
        r = client.post("/api/config", json={
            "base_url": "http://127.0.0.1:1234/v1", "api_key": "", "model": "gvs",
            "profile": "code", "tests": "3 => 9\n1 => 1", "max_iters": 6})
        assert r.status_code == 200, r.text
        r = client.get("/api/config")
        assert r.json()["model"] == "gvs"

        # give the task via chat
        r = client.post("/api/chat", json={"message": "Read an int from stdin, print its square."})
        assert r.status_code == 200, r.text
        # wait for the manager's streamed reply
        s = wait_for(client, lambda x: len(x["current"]["chat"]) >= 2)
        roles = [m["role"] for m in s["current"]["chat"]]
        assert "user" in roles and "manager" in roles, roles

        # start the run
        r = client.post("/api/run", json={})
        assert r.status_code == 200, r.text
        # wait for completion
        s = wait_for(client, lambda x: x["run"]["state"] in ("done", "stopped", "error"))
        assert s["run"]["state"] in ("done", "stopped"), s["run"]
        assert s["current"]["ws"], "no workspace"
        assert len(s["current"]["calls"]) >= 3, "expected several model calls"
        assert s["current"]["ledger"].get("artifact"), "no artifact"
        assert s["current"]["tokens"]["calls"] >= 3
        # the manager posted progress into the chat
        mgr = [m for m in s["current"]["chat"] if m["role"] == "manager"]
        assert len(mgr) >= 2, "expected manager progress messages"
        # saved run is listed
        r = client.get("/api/runs")
        assert any(run["slug"] == os.path.basename(s["current"]["ws"])
                   for run in r.json()["runs"]), "run not in list"
    print("REST flow: OK")


def test_websocket_stream():
    st = ServerState()
    st.cfg.model = "gvs"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9"
    st.cfg.max_iters = 6
    app = create_app(st)
    with TestClient(app) as client:
        client.post("/api/chat", json={"message": "Read an int, print its square."})
        wait_for(client, lambda x: len(x["current"]["chat"]) >= 2)
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "state", first.get("type")
            # start the run while the socket is open
            client.post("/api/run", json={})
            types = []
            t0 = time.time()
            while time.time() - t0 < 40:
                ev = ws.receive_json()
                types.append(ev["type"])
                if ev["type"] == "done":
                    break
            assert "call" in types, f"no call events: {types}"
            assert "ledger" in types, f"no ledger events: {types}"
            assert "chat_message" in types, f"no chat events: {types}"
            assert "done" in types, f"no done event: {types}"
    print("WebSocket stream: OK")


def test_manager_thinking_and_run():
    st = ServerState()
    st.cfg.model = "gvs"
    st.cfg.profile = "general"
    st.cfg.max_iters = 3
    app = create_app(st)

    def thinking_stream(model, messages, temperature=0.2, meta=None):
        yield ("reasoning", "Let me think. ")
        yield ("reasoning", "I should start the run. ")
        yield ("content", "Starting now.\n")
        yield ("content", "RUN: Read an int and print its square.")
        if meta is not None:
            meta["finish_reason"] = "stop"
            meta["content"] = "Starting now.\nRUN: Read an int and print its square."
            meta["reasoning"] = "Let me think. I should start the run. "

    orig = server.compat_chat_stream
    server.compat_chat_stream = thinking_stream
    try:
        with TestClient(app) as client:
            client.post("/api/chat", json={"message": "Please start the run."})
            # the manager message should carry the captured reasoning
            def has_reasoning(s):
                return any(m.get("role") == "manager" and m.get("reasoning")
                           for m in s["current"]["chat"])
            wait_for(client, has_reasoning)
            # the RUN: marker should have kicked off a run (state leaves idle)
            wait_for(client, lambda s: s["run"]["state"] != "idle")
    finally:
        server.compat_chat_stream = orig
    print("Manager thinking + RUN dispatch: OK")


def test_open_and_continue():
    # seed a run, then reopen it and continue
    st = ServerState()
    st.cfg.model = "gvs"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9"
    st.cfg.max_iters = 2  # low so it may not fully finish
    app = create_app(st)
    with TestClient(app) as client:
        client.post("/api/chat", json={"message": "Read an int, print its square."})
        wait_for(client, lambda x: len(x["current"]["chat"]) >= 2)
        client.post("/api/run", json={"fresh": True})
        s = wait_for(client, lambda x: x["run"]["state"] in ("done", "stopped", "error"))
        slug = os.path.basename(s["current"]["ws"])
        # open the saved run in a fresh state
        st2 = ServerState()
        st2.cfg.model = "gvs"
        st2.cfg.profile = "code"
        st2.cfg.tests = "3 => 9"
        st2.cfg.max_iters = 6
        app2 = create_app(st2)
        with TestClient(app2) as client2:
            r = client2.post("/api/open", json={"slug": slug})
            assert r.status_code == 200, r.text
            state = r.json()["state"]
            assert state["current"]["ws"], "open failed"
            assert len(state["current"]["calls"]) >= 1, "no calls loaded"
            # continue it
            r = client2.post("/api/continue", json={})
            assert r.status_code == 200, r.text
            s2 = wait_for(client2, lambda x: x["run"]["state"] in ("done", "stopped", "error"))
            assert s2["run"]["state"] in ("done", "stopped"), s2["run"]
    print("open + continue: OK")


def test_file_browser():
    st = ServerState()
    st.cfg.model = "gvs"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9"
    st.cfg.max_iters = 6
    app = create_app(st)
    with TestClient(app) as client:
        # no workspace yet -> empty list
        r = client.get("/api/files")
        assert r.status_code == 200 and r.json()["files"] == [], r.text
        # seed a run so the workspace has files
        client.post("/api/chat", json={"message": "Read an int from stdin, print its square."})
        wait_for(client, lambda x: len(x["current"]["chat"]) >= 2)
        client.post("/api/run", json={})
        s = wait_for(client, lambda x: x["run"]["state"] in ("done", "stopped", "error"))
        assert s["run"]["state"] in ("done", "stopped"), s["run"]
        # list the workspace files
        r = client.get("/api/files")
        assert r.status_code == 200, r.text
        names = {f["name"] for f in r.json()["files"]}
        assert "task.md" in names, names
        assert "plan.md" in names, names
        assert "transcript.jsonl" in names, names
        # read one of them
        r = client.get("/api/file", params={"name": "task.md"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "task.md"
        assert "square" in body["content"], body["content"]
        assert body["kind"] == "md"
        # path traversal is rejected
        r = client.get("/api/file", params={"name": "../config.json"})
        assert r.status_code == 400, r.status_code
        r = client.get("/api/file", params={"name": ".."})
        assert r.status_code == 400, r.status_code
        # missing file -> 404
        r = client.get("/api/file", params={"name": "nope.md"})
        assert r.status_code == 404, r.status_code
        # nested files (workers may write to subdirs) are listed and readable
        ws = s["current"]["ws"]
        os.makedirs(os.path.join(ws, "notes"), exist_ok=True)
        with open(os.path.join(ws, "notes", "research.md"), "w", encoding="utf-8") as f:
            f.write("nested research notes")
        r = client.get("/api/files")
        names = {f["name"] for f in r.json()["files"]}
        assert "notes/research.md" in names, names
        r = client.get("/api/file", params={"name": "notes/research.md"})
        assert r.status_code == 200, r.text
        assert "nested research notes" in r.json()["content"], r.json()["content"]
    print("file browser: OK")


def test_file_browser_tool_written():
    """A worker that emits a write_file tool call produces a file that appears
    in /api/files with the tool-written content (the B-full chain)."""
    state = {"worker_calls": 0}

    def tool_mock(model, messages, temperature=0.2, meta=None):
        if meta is not None:
            meta["provider"] = "mock:tool"
            meta["finish_reason"] = "stop"
        sysmsg = messages[0]["content"]
        if "WORKER subagent" in sysmsg:
            state["worker_calls"] += 1
            if state["worker_calls"] == 1:
                return ('```tool\n{"name": "write_file", "args": {"path": "result.md", '
                        '"content": "# Tool Written Deliverable\\n\\nMade via write_file."}}\n```')
            return "### STATUS\nsolved\n### NEXT\nnone\n### NOTES\n- Wrote via tool."
        if "FIRST WORKER" in sysmsg:
            return "### NOTES\n- A\n### NEXT\n- Try A"
        if "overarching plan" in sysmsg:
            return "### PLAN\nPlan.\n### TASKS\n- [todo] Produce it"
        if "CUT OFF" in sysmsg:
            return "Partway."
        return "### STATUS\ndone\n### NEXT\nnone\n### TASKS\n- [done] Produce it\n### UPDATE\nDone."

    st = ServerState()
    st.cfg.model = "gvs"
    st.cfg.profile = "general"
    st.cfg.max_iters = 3
    app = create_app(st)
    orig = chatmod.compat_chat
    chatmod.compat_chat = tool_mock
    try:
        with TestClient(app) as client:
            client.post("/api/run", json={"task": "Write a one-paragraph deliverable about testing."})
            s = wait_for(client, lambda x: x["run"]["state"] in ("done", "stopped", "error"))
            assert s["run"]["state"] == "done", s["run"]
            r = client.get("/api/files")
            names = {f["name"] for f in r.json()["files"]}
            assert "result.md" in names, names
            r = client.get("/api/file", params={"name": "result.md"})
            assert r.status_code == 200, r.text
            assert "Tool Written Deliverable" in r.json()["content"], r.json()["content"]
    finally:
        chatmod.compat_chat = orig
    print("file browser (tool-written): OK")


if __name__ == "__main__":
    test_rest_flow()
    test_websocket_stream()
    test_manager_thinking_and_run()
    test_open_and_continue()
    test_file_browser()
    test_file_browser_tool_written()
    print("\nALL WEB BACKEND TESTS PASSED")
