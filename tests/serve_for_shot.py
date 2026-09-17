"""Start the web server + a role-aware mock OpenAI server, drive a full mock run,
write the web port to a file, then stay alive so a screenshot can be taken.

Run:  python tests/serve_for_shot.py
Prints "READY <port>" and writes the port to <temp>/gvs_web_port.txt.
"""

import os
import sys
import json
import time
import socket
import tempfile
import threading

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
        meta = {}
        text = mock_chat("mock:gvs", body["messages"], body.get("temperature", 0.2), meta)
        if body.get("stream"):
            def gen():
                for tok in text:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': tok}}]})}\n\n"
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
            return
    raise RuntimeError("server did not start")


def main():
    tmp = tempfile.mkdtemp(prefix="gvs_shot_")
    server.WS_ROOT = os.path.join(tmp, "runs", "ws")
    server.CONFIG_PATH = os.path.join(tmp, "config.json")

    openai_port = free_port()
    start_uvicorn(make_openai_server(), openai_port)

    st = ServerState()
    st.cfg.base_url = f"http://127.0.0.1:{openai_port}/v1"
    st.cfg.model = "gvs-mock"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9\n1 => 1"
    st.cfg.max_iters = 6
    web_port = free_port()
    start_uvicorn(create_app(st), web_port)

    base = f"http://127.0.0.1:{web_port}"
    client = httpx.Client(base_url=base, timeout=60)
    client.post("/api/config", json={
        "base_url": f"http://127.0.0.1:{openai_port}/v1", "api_key": "",
        "model": "gvs-mock", "profile": "code", "tests": "3 => 9\n1 => 1",
        "max_iters": 6, "max_tokens": 8192, "timeout": 900})
    client.post("/api/chat", json={"message": "Read an int from stdin, print its square."})
    t0 = time.time()
    while time.time() - t0 < 40:
        s = client.get("/api/state").json()
        if len(s["current"]["chat"]) >= 2:
            break
        time.sleep(0.1)
    client.post("/api/run", json={})
    t0 = time.time()
    while time.time() - t0 < 60:
        s = client.get("/api/state").json()
        if s["run"]["state"] in ("done", "stopped", "error"):
            break
        time.sleep(0.2)
    assert s["run"]["state"] in ("done", "stopped"), s["run"]

    # record a worker call index for the zoom-in shot
    calls = s["current"]["calls"]
    worker_idx = next((i for i, c in enumerate(calls) if c["role"] == "worker"), 0)
    port_file = os.path.join(tempfile.gettempdir(), "gvs_web_port.txt")
    with open(port_file, "w") as f:
        f.write(f"{web_port}\n{worker_idx}")

    print(f"READY {web_port} worker={worker_idx}", flush=True)
    time.sleep(3600)  # stay alive for screenshots


if __name__ == "__main__":
    main()
