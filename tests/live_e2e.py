"""Full live end-to-end test (no TestClient):

  * a role-aware mock OpenAI-compatible server (real HTTP, streaming + not)
  * the real web server (create_app) pointed at it
  * drive it over HTTP: config -> chat (task) -> run -> poll to done
  * verify the artifact, calls, and manager chat progress

Run:  python tests/live_e2e.py
"""

import os
import sys
import json
import time
import socket
import threading
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uvicorn
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from gvs_tui.chat import mock_chat
import gvs_tui.web.server as server
from gvs_tui.web.server import create_app, ServerState


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_openai_server():
    app = FastAPI()

    @app.get("/v1/models")
    def models():
        return {"data": [{"id": "gvs-mock"}]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        messages = body["messages"]
        temperature = body.get("temperature", 0.2)
        meta = {}
        text = mock_chat("mock:gvs", messages, temperature, meta)
        if body.get("stream"):
            def gen():
                for tok in text:
                    chunk = {"choices": [{"delta": {"content": tok}}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            "id": "x", "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": text},
                        "finish_reason": meta.get("finish_reason", "stop")}],
            "usage": {"prompt_tokens": meta.get("prompt_tokens", 10),
                      "completion_tokens": meta.get("completion_tokens", len(text))},
        }
    return app


def start_uvicorn(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}", timeout=1)
            return
        except httpx.ConnectError:
            time.sleep(0.1)
        except Exception:
            return  # got a response (even 404) -> up
    raise RuntimeError("server did not start")


def main():
    tmp = tempfile.mkdtemp(prefix="gvs_live_e2e_")
    server.WS_ROOT = os.path.join(tmp, "runs", "ws")
    server.CONFIG_PATH = os.path.join(tmp, "config.json")

    # 1. mock OpenAI server
    openai_port = free_port()
    start_uvicorn(make_openai_server(), openai_port)

    # 2. web server pointed at the mock
    st = ServerState()
    st.cfg.base_url = f"http://127.0.0.1:{openai_port}/v1"
    st.cfg.api_key = ""
    st.cfg.model = "gvs-mock"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9\n1 => 1"
    st.cfg.max_iters = 6
    web_port = free_port()
    start_uvicorn(create_app(st), web_port)

    base = f"http://127.0.0.1:{web_port}"
    client = httpx.Client(base_url=base, timeout=60)

    # 3. config (also applied to the compat endpoint)
    r = client.post("/api/config", json={
        "base_url": f"http://127.0.0.1:{openai_port}/v1", "api_key": "",
        "model": "gvs-mock", "profile": "code", "tests": "3 => 9\n1 => 1",
        "max_iters": 6, "max_tokens": 8192, "timeout": 900})
    assert r.status_code == 200, r.text

    # 4. task via chat
    r = client.post("/api/chat", json={"message": "Read an int from stdin, print its square."})
    assert r.status_code == 200, r.text

    # wait for the manager's streamed reply
    t0 = time.time()
    while time.time() - t0 < 40:
        s = client.get("/api/state").json()
        if len(s["current"]["chat"]) >= 2:
            break
        time.sleep(0.1)
    roles = [m["role"] for m in s["current"]["chat"]]
    assert "user" in roles and "manager" in roles, roles

    # 5. run
    r = client.post("/api/run", json={})
    assert r.status_code == 200, r.text

    # poll to completion
    t0 = time.time()
    while time.time() - t0 < 60:
        s = client.get("/api/state").json()
        if s["run"]["state"] in ("done", "stopped", "error"):
            break
        time.sleep(0.2)
    assert s["run"]["state"] in ("done", "stopped"), s["run"]
    assert s["current"]["ws"], "no workspace"
    assert len(s["current"]["calls"]) >= 3, "expected several model calls"
    assert s["current"]["ledger"].get("artifact"), "no artifact produced"
    mgr = [m for m in s["current"]["chat"] if m["role"] == "manager"]
    assert len(mgr) >= 2, "expected manager progress messages"
    # artifact is real python
    art = s["current"]["ledger"]["artifact"]
    assert "def" in art or "print" in art, "artifact doesn't look like code"

    # 6. saved run listed
    r = client.get("/api/runs")
    assert any(x["slug"] == os.path.basename(s["current"]["ws"]) for x in r.json()["runs"])

    print("LIVE E2E: OK")
    print(f"  calls={len(s['current']['calls'])}  "
          f"tokens={s['current']['tokens']['prompt_tokens']}+{s['current']['tokens']['completion_tokens']}  "
          f"artifact={len(art)} chars")
    print("  artifact head:", art.strip().splitlines()[0] if art.strip() else "(empty)")


if __name__ == "__main__":
    main()
    print("\nLIVE E2E PASSED")
