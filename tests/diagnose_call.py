"""Focused diagnosis: does the config persist, and do calls actually fire?

Starts a mock OpenAI-compatible server (logs every request), starts the web
server, saves a config the way the frontend does, sends a chat message, then
starts a run — and reports whether config.json was written and how many
requests the LLM endpoint actually received.

Run:  python tests/diagnose_call.py
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

REQ_LOG = []


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_openai_server():
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        REQ_LOG.append({"model": body.get("model"), "max_tokens": body.get("max_tokens"),
                        "stream": bool(body.get("stream"))})
        meta = {}
        text = mock_chat("mock:gvs", body["messages"], body.get("temperature", 0.2), meta)
        if body.get("stream"):
            def gen():
                for tok in text:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': tok}}]})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {"id": "x", "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": text},
                            "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": len(text)}}
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
    tmp = tempfile.mkdtemp(prefix="gvs_diag_")
    server.WS_ROOT = os.path.join(tmp, "runs", "ws")
    server.CONFIG_PATH = os.path.join(tmp, "config.json")

    openai_port = free_port()
    start_uvicorn(make_openai_server(), openai_port)

    st = ServerState()
    st.cfg.base_url = f"http://127.0.0.1:{openai_port}/v1"
    st.cfg.model = "test-model"
    st.cfg.profile = "code"
    st.cfg.tests = "3 => 9"
    web_port = free_port()
    start_uvicorn(create_app(st), web_port)

    base = f"http://127.0.0.1:{web_port}"
    client = httpx.Client(base_url=base, timeout=60)

    # --- save the config exactly the way the frontend does ---
    r = client.post("/api/config", json={
        "base_url": f"http://127.0.0.1:{openai_port}/v1", "api_key": "",
        "model": "test-model", "temperature": None, "max_tokens": 131072,
        "timeout": 900, "profile": "code", "tests": "3 => 9", "max_iters": 6})
    print("config save ->", r.status_code, r.json())
    cfg_file = server.CONFIG_PATH
    if os.path.exists(cfg_file):
        with open(cfg_file, encoding="utf-8") as f:
            c = json.load(f)
        print("config.json persisted -> model=%r max_tokens=%s" % (c["model"], c["max_tokens"]))
    else:
        print("config.json NOT persisted")

    # --- chat: does a call fire? ---
    REQ_LOG.clear()
    client.post("/api/chat", json={"message": "Read an int, print its square."})
    time.sleep(3)
    print("chat -> calls fired:", len(REQ_LOG))
    if REQ_LOG:
        print("   first call:", REQ_LOG[0])

    # --- run: do calls fire? ---
    REQ_LOG.clear()
    client.post("/api/run", json={})
    t0 = time.time()
    s = {}
    while time.time() - t0 < 30:
        s = client.get("/api/state").json()
        if s["run"]["state"] in ("done", "stopped", "error"):
            break
        time.sleep(0.2)
    print("run -> calls fired:", len(REQ_LOG), " state:", s["run"]["state"])

    ok = (os.path.exists(cfg_file) and len(REQ_LOG) >= 1 and s["run"]["state"] == "done")
    print("DIAGNOSIS", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
