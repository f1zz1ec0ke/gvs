"""Verify the real HTTP path of the compat provider against a local
OpenAI-compatible server (non-streaming, streaming, and /models).

Run:  python tests/test_compat_http.py
"""

import os
import sys
import json
import time
import socket
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def make_openai_server():
    app = FastAPI()

    @app.get("/v1/models")
    def models():
        return {"data": [{"id": "test-1"}, {"id": "test-2"}]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        if body.get("stream"):
            def gen():
                for tok in ["Hello", " world", "!"]:
                    chunk = {"choices": [{"delta": {"content": tok}}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            "id": "x", "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "Hello world!"},
                        "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
    return app


def start_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    import httpx
    for _ in range(80):
        try:
            httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1)
            return port
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("mock OpenAI server did not start")


def test_compat_http():
    import gvs_tui.chat as chatmod
    port = start_server(make_openai_server())
    base = f"http://127.0.0.1:{port}/v1"
    chatmod.set_compat_endpoint(base, "")

    # non-streaming
    meta = {}
    out = chatmod.compat_chat("test-1", [{"role": "user", "content": "hi"}], meta=meta)
    assert out == "Hello world!", out
    assert meta.get("completion_tokens") == 3, meta

    # streaming
    meta2 = {}
    parts = list(chatmod.compat_chat_stream("test-1", [{"role": "user", "content": "hi"}], meta=meta2))
    assert "".join(parts) == "Hello world!", parts
    assert meta2.get("finish_reason") == "stop", meta2

    # fetch models
    models = chatmod.fetch_models(base, "")
    assert "test-1" in models and "test-2" in models, models

    print("compat HTTP (non-stream + stream + models): OK")


if __name__ == "__main__":
    test_compat_http()
    print("\nCOMPAT HTTP TESTS PASSED")
