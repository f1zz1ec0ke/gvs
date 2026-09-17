"""Provider-agnostic chat layer.

Adapted from GVS5H's ``orchestrator.py``: the same model can be reached through
several transports (Ollama local, OpenAI-compatible endpoints, Anthropic,
OpenRouter, the Claude Code CLI). ``chat(model, messages, ...)`` dispatches on a
``provider:model`` prefix and returns the assistant text. A ``meta`` dict is
filled with usage/finish info for the transcript.

A ``mock:`` provider is included so the full orchestration loop can run and be
verified with no API key: it is a deterministic fake model that reads the role
out of the system prompt and answers in the exact sectioned format the loop
parses.
"""

import os
import re
import json
import time
import threading
import subprocess
import urllib.request
import urllib.error

# --- configuration (env-overridable, same names as GVS5H) -----------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
REQUEST_TIMEOUT = int(os.environ.get("ESCALATION_TIMEOUT", "1200"))
NUM_CTX = int(os.environ.get("ESCALATION_NUM_CTX", "16384"))
THINK = os.environ.get("ESCALATION_THINK", "0") == "1"

CLOUD_TIMEOUT = int(os.environ.get("ESCALATION_CLOUD_TIMEOUT", "120"))
CLOUD_MAX_TOKENS = int(os.environ.get("ESCALATION_CLOUD_MAX_TOKENS", "8000"))
CLAUDE_TIMEOUT = int(os.environ.get("ESCALATION_CLAUDE_TIMEOUT", "1800"))
CLAUDE_ATTEMPTS = int(os.environ.get("ESCALATION_CLAUDE_ATTEMPTS", "3"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_REASONING = os.environ.get("ESCALATION_OPENAI_REASONING", "")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = os.environ.get("ESCALATION_OPENAI_BASE", "https://api.groq.com/openai/v1/chat/completions")
GROQ_REASONING = os.environ.get("ESCALATION_GROQ_REASONING", "none")

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_URL = os.environ.get(
    "ESCALATION_DASHSCOPE_BASE",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions")
DASHSCOPE_THINKING_BUDGET = int(os.environ.get("ESCALATION_DASHSCOPE_THINKING_BUDGET", "0"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MAX_OUTPUT = 128000
ANTHROPIC_EFFORT = "high"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_REASONING = os.environ.get("ESCALATION_OR_REASONING", "none")
OPENROUTER_PROVIDERS = [p.strip() for p in os.environ.get("ESCALATION_OR_PROVIDERS", "").split(",") if p.strip()]

# --- config-driven OpenAI-compatible endpoint (the web GUI's provider) ----
# Pointed at any OpenAI-compatible server (LM Studio, vLLM, Ollama's OpenAI
# endpoint, LiteLLM, real OpenAI). The web server calls set_compat_endpoint()
# with the user's config; `compat:<model>` then routes through it.
_COMPAT_BASE_URL = os.environ.get("COMPAT_BASE_URL", "http://127.0.0.1:1234/v1")
_COMPAT_API_KEY = os.environ.get("COMPAT_API_KEY", "")


# --- first-party providers -------------------------------------------------

def ollama_chat(model, messages, temperature=0.2, meta=None):
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": THINK,
        "options": {"temperature": temperature, "num_ctx": NUM_CTX},
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=data, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                out = json.loads(r.read())
            if meta is not None:
                meta["finish_reason"] = out.get("done_reason")
                meta["completion_tokens"] = out.get("eval_count")
                meta["prompt_tokens"] = out.get("prompt_eval_count")
                meta["reasoning"] = (out.get("message") or {}).get("thinking") or ""
            return out["message"]["content"]
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    return ""


def openai_chat(url, api_key, model, messages, temperature=0.2, extra=None, meta=None,
               token_param="max_tokens", send_temperature=True):
    base = {"model": model, "messages": messages, "stream": False}
    if send_temperature:
        base["temperature"] = temperature
    if CLOUD_MAX_TOKENS > 0:
        base[token_param] = CLOUD_MAX_TOKENS
    if extra:
        base.update(extra)
    prov0 = dict(base.get("provider") or {})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "gvs-tui/0.1",
    }

    def _do(box, data):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as r:
                box["out"] = json.loads(r.read())
        except urllib.error.HTTPError as e:
            box["err"] = e
            box["code"] = e.code
            try:
                box["body"] = e.read().decode()[:300]
            except Exception:
                box["body"] = ""
        except Exception as e:
            box["err"] = e

    ATTEMPTS = 16
    only = list(prov0.get("only") or [])
    cur_max = base.get(token_param)
    ignore, last, got_response = [], "", False
    for attempt in range(ATTEMPTS):
        body = dict(base)
        if cur_max:
            body[token_param] = cur_max
        prov = dict(prov0)
        if only:
            k = attempt % len(only)
            prov["order"] = only[k:] + only[:k]
        if ignore:
            prov["ignore"] = list(ignore)
        if prov:
            body["provider"] = prov
        box = {}
        th = threading.Thread(target=_do, args=(box, json.dumps(body).encode()), daemon=True)
        th.start()
        th.join(CLOUD_TIMEOUT + 5)
        if th.is_alive():
            if meta is not None:
                meta.setdefault("discarded", []).append(
                    {"attempt": attempt + 1, "why": f"hard timeout >{CLOUD_TIMEOUT}s (no response)"})
            continue
        out = box.get("out")
        if out is None:
            e = box.get("err")
            if meta is not None:
                meta.setdefault("discarded", []).append(
                    {"attempt": attempt + 1, "why": f"{type(e).__name__}: {str(e)[:200]}",
                     "http_code": box.get("code"), "body": box.get("body")})
            body_txt = (box.get("body") or "").lower()
            ctx_err = any(s in body_txt for s in (
                "context", "maximum context length", "too long", "exceeds", "max_model_len"))
            if box.get("code") == 400 and ctx_err and cur_max and cur_max > 40000:
                cur_max = max(40000, cur_max - 24000)
                continue
            if box.get("code") == 400 and ctx_err:
                break
            if box.get("code") in (400, 401, 402, 403, 404) and not ctx_err:
                break
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                ra = e.headers.get("retry-after")
                try:
                    wait = float(ra) if ra else min(30.0, 5.0 * (attempt + 1))
                except ValueError:
                    wait = min(30.0, 5.0 * (attempt + 1))
                time.sleep(wait + 1)
                continue
            time.sleep(3 * (attempt + 1))
            continue
        served = out.get("provider")
        choices = out.get("choices")
        if not choices:
            if meta is not None:
                meta.setdefault("discarded", []).append(
                    {"attempt": attempt + 1, "provider": served, "why": "no choices in response"})
            if served:
                ignore.append(served)
            time.sleep(2)
            continue
        got_response = True
        choice = choices[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if not content:
            content = reasoning
        usage = out.get("usage") or {}
        ntok = usage.get("completion_tokens")
        if meta is not None:
            meta["finish_reason"] = finish
            meta["completion_tokens"] = ntok
            meta["prompt_tokens"] = usage.get("prompt_tokens")
            meta["reasoning"] = reasoning
            meta["provider"] = served
            meta["attempts"] = attempt + 1
        cap_used = cur_max or CLOUD_MAX_TOKENS
        clamped = finish == "length" and cap_used and (ntok or 0) < 0.9 * cap_used
        if content.strip() and not clamped:
            return content
        if served:
            ignore.append(served)
        last = content or last
        time.sleep(1)
    if meta is not None and not last.strip():
        meta["infra_exhausted"] = True
    return last


def claude_cli_chat(model, messages, temperature=0.2, meta=None):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--no-session-persistence", "--tools", "", "--model", model]
    if system:
        cmd += ["--append-system-prompt", system]
    for attempt in range(CLAUDE_ATTEMPTS):
        try:
            proc = subprocess.run(cmd, input=body, capture_output=True, text=True,
                                  timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            if meta is not None:
                meta.setdefault("discarded", []).append(
                    {"attempt": attempt + 1, "why": f"timeout >{CLAUDE_TIMEOUT}s (no response)"})
            continue
        text, usage, stop, err = "", {}, None, False
        thinking, blocks = [], 0
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "thinking":
                        blocks += 1
                        if b.get("thinking"):
                            thinking.append(b["thinking"])
            if ev.get("type") == "result":
                text = ev.get("result") or ""
                usage = ev.get("usage") or {}
                stop = ev.get("stop_reason")
                err = bool(ev.get("is_error"))
        if meta is not None:
            meta["finish_reason"] = ("length" if stop == "max_tokens"
                                     else "error" if err else stop or "stop")
            meta["completion_tokens"] = usage.get("output_tokens")
            meta["prompt_tokens"] = usage.get("input_tokens")
            meta["reasoning"] = "\n\n".join(thinking)
            meta["thinking_blocks"] = blocks
            meta["attempts"] = attempt + 1
        if text.strip() and not err:
            return text
        if meta is not None:
            meta.setdefault("discarded", []).append(
                {"attempt": attempt + 1, "why": f"empty/error (stop={stop} rc={proc.returncode})"})
        time.sleep(3 * (attempt + 1))
    if meta is not None:
        meta["infra_exhausted"] = True
    return ""


def anthropic_chat(model, messages, temperature=0.2, meta=None):
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=CLOUD_TIMEOUT)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    msgs = [{"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] != "system"]
    want = CLOUD_MAX_TOKENS or ANTHROPIC_MAX_OUTPUT
    max_out = min(want, ANTHROPIC_MAX_OUTPUT)
    for attempt in range(CLAUDE_ATTEMPTS):
        try:
            kw = {"model": model, "max_tokens": max_out, "messages": msgs,
                  "thinking": {"type": "adaptive", "display": "summarized"},
                  "output_config": {"effort": ANTHROPIC_EFFORT}}
            if system:
                kw["system"] = system
            with client.messages.stream(**kw) as stream:
                msg = stream.get_final_message()
        except Exception as e:  # noqa: BLE001 - recorded, then retried
            if meta is not None:
                meta.setdefault("discarded", []).append(
                    {"attempt": attempt + 1, "why": f"{type(e).__name__}: {str(e)[:200]}"})
            time.sleep(3 * (attempt + 1))
            continue
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        thinking = "\n\n".join(getattr(b, "thinking", "") or ""
                               for b in msg.content if getattr(b, "type", "") == "thinking")
        if meta is not None:
            meta["finish_reason"] = "length" if msg.stop_reason == "max_tokens" else msg.stop_reason
            meta["completion_tokens"] = msg.usage.output_tokens
            meta["prompt_tokens"] = msg.usage.input_tokens
            meta["reasoning"] = thinking
            meta["reasoning_is_summary"] = True
            meta["attempts"] = attempt + 1
        return text
    if meta is not None:
        meta["infra_exhausted"] = True
    return ""


# --- mock provider (deterministic fake model, no key required) -------------
#
# The mock infers its role from the system prompt -- exactly what a real model
# would do -- and answers in the sectioned format the loop parses. It is
# stateless but progress-aware: the manager reports "done" once the ledger
# already holds a non-empty artifact, so the loop terminates cleanly.

def _mock_section(text, header):
    m = re.search(rf"^### {header}\s*$(.*?)(?=^### |\Z)", text, re.DOTALL | re.MULTILINE)
    return (m.group(1).strip() if m else "")


def _mock_current_artifact(user):
    m = re.search(r"CURRENT (?:SOLUTION|ANSWER|ARTIFACT):\n(.*?)\n\nNOTES:", user, re.DOTALL)
    if not m:
        m = re.search(r"CURRENT (?:SOLUTION|ANSWER|ARTIFACT):\n(.*)", user, re.DOTALL)
    if not m:
        return ""
    val = m.group(1).strip()
    return "" if val in ("", "(none yet)") else val


def _mock_first_todo(user):
    m = re.search(r"CURRENT TASK LIST:\n(.*?)\n\nLATEST", user, re.DOTALL)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        t = line.strip()
        if re.match(r"^[-*]\s*\[\s*todo", t, re.I):
            return re.sub(r"^[-*]\s*\[\s*todo\s*\]\s*", "", t, flags=re.I).strip()
    return ""


def _mock_task_of(user):
    m = re.search(r"YOUR TASK:\s*(.*)", user, re.DOTALL)
    return (m.group(1).strip()[:120] if m else "the task")


def mock_chat(model, messages, temperature=0.2, meta=None):
    sysmsg = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    if meta is not None:
        meta["finish_reason"] = "stop"
        meta["completion_tokens"] = 140
        meta["prompt_tokens"] = 90
        meta["provider"] = "mock"
        meta["reasoning"] = ""

    # role detection from the system prompt
    if "CUT OFF" in sysmsg:  # cutoff summarizer
        return ("The worker was pursuing a straightforward implementation, established the "
                "overall structure, and got cut off before finishing the final checks. "
                "What remains is wiring up the last edge case and verifying the output.")
    if "FIRST WORKER" in sysmsg:  # ideation
        return ("### NOTES\n"
                "Core difficulty: the task has several plausible decompositions. Two distinct "
                "routes stand out: a direct construction and a reduction to a smaller sub-problem. "
                "Pitfalls: off-by-one boundaries and unhandled empty inputs.\n"
                "### NEXT\n"
                "- Try the direct construction first\n"
                "- Keep the reduction approach as a fallback\n"
                "- Verify edge cases (empty input, single element)")
    if "orchestrator and manager" in sysmsg:  # manage
        cur = _mock_current_artifact(user)
        if cur:
            return ("### STATUS\ndone\n"
                    "### NEXT\nnone\n"
                    "### TASKS\n"
                    "- [done] Draft the overarching plan\n"
                    "- [done] Implement the working solution\n"
                    "- [done] Verify the result\n"
                    "### UPDATE\nThe artifact is in place and looks complete, so I'm marking the task done.")
        nxt = _mock_first_todo(user) or "Implement the working solution"
        return ("### STATUS\ncontinue\n"
                f"### NEXT\n{nxt}\n"
                "### TASKS\n"
                "- [done] Draft the overarching plan\n"
                "- [todo] Implement the working solution\n"
                "- [todo] Verify the result\n"
                f"### UPDATE\nPicking up '{nxt}' next and sending it to a worker.")
    if "orchestrator (manager) of a small team" in sysmsg:  # plan
        return ("### PLAN\n"
                "Break the task into a short sequence of concrete steps, implement the most "
                "promising one, verify it, and iterate on any failure. Keep the notes current so "
                "each fresh worker can resume without re-deriving prior work.\n"
                "### TASKS\n"
                "- [todo] Draft the overarching plan\n"
                "- [todo] Implement the working solution\n"
                "- [todo] Verify the result")
    if "WORKER subagent" in sysmsg:  # worker
        if "### CODE" in sysmsg:
            task = _mock_task_of(user)
            code = (
                "import sys\n\n"
                f"# mock solution for: {task}\n"
                "def main():\n"
                "    data = sys.stdin.read().strip()\n"
                "    try:\n"
                "        n = int(data)\n"
                "        print(n * n)\n"
                "    except ValueError:\n"
                "        print(data)\n\n"
                "if __name__ == \"__main__\":\n"
                "    main()\n"
            )
            return ("### CODE\n```python\n" + code + "```\n"
                    "### NOTES\n- **Approach:** direct stdin/stdout program; square integer input, "
                    "echo anything else.\n- **Status:** implemented and self-consistent.\n"
                    "### NEXT\nnone\n"
                    "### STATUS\nsolved")
        task = _mock_task_of(user)
        return ("### ANSWER\n"
                f"Working through the task ({task}): the key step is to reduce it to a concrete "
                "computation, carry it through, and check the boundary cases. The result is "
                "consistent under that reduction.\n"
                "ANSWER: 42\n"
                "### NOTES\n- **Approach:** reduction to a concrete computation.\n"
                "- **Checks:** boundary cases verified.\n"
                "### NEXT\nnone\n"
                "### STATUS\nsolved")
    return "OK"


# --- config-driven OpenAI-compatible endpoint ------------------------------

def set_compat_endpoint(base_url, api_key=""):
    """Point the ``compat:`` provider at an OpenAI-compatible server."""
    global _COMPAT_BASE_URL, _COMPAT_API_KEY
    _COMPAT_BASE_URL = (base_url or "").rstrip("/")
    _COMPAT_API_KEY = api_key or ""


def compat_endpoint():
    return _COMPAT_BASE_URL, _COMPAT_API_KEY


def _completions_url(base_url):
    b = (base_url or _COMPAT_BASE_URL).rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def compat_chat(model, messages, temperature=0.2, meta=None):
    """Non-streaming call through the configured OpenAI-compatible endpoint."""
    return openai_chat(_completions_url(_COMPAT_BASE_URL), _COMPAT_API_KEY, model,
                       messages, temperature, None, meta,
                       token_param="max_tokens", send_temperature=True)


def compat_chat_stream(model, messages, temperature=0.2, meta=None):
    """Streaming call; yields content deltas. Fills ``meta`` on completion."""
    import httpx

    url = _completions_url(_COMPAT_BASE_URL)
    headers = {"Content-Type": "application/json"}
    if _COMPAT_API_KEY:
        headers["Authorization"] = f"Bearer {_COMPAT_API_KEY}"
    body = {"model": model, "messages": messages, "stream": True,
            "temperature": temperature}
    if CLOUD_MAX_TOKENS > 0:
        body["max_tokens"] = CLOUD_MAX_TOKENS
    parts = []
    with httpx.Client(timeout=CLOUD_TIMEOUT) as client:
        with client.stream("POST", url, json=body, headers=headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = ev.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if delta:
                    parts.append(delta)
                    yield delta
    if meta is not None:
        meta["finish_reason"] = "stop"
        meta["provider"] = "compat"
        meta["reasoning"] = ""


def fetch_models(base_url=None, api_key=None, timeout=15):
    """List model ids from an OpenAI-compatible ``/models`` endpoint."""
    import httpx

    base = (base_url or _COMPAT_BASE_URL).rstrip("/")
    key = api_key if api_key is not None else _COMPAT_API_KEY
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with httpx.Client(timeout=timeout) as client:
        r = client.get(base + "/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    return [m.get("id") for m in (data.get("data") or []) if m.get("id")]


# --- dispatch ---------------------------------------------------------------

def chat(model, messages, temperature=0.2, meta=None):
    """Call ``model`` (``provider:model``) and return the assistant text."""
    if model.startswith("mock:"):
        return mock_chat(model, messages, temperature, meta)
    if model.startswith("compat:"):
        return compat_chat(model[len("compat:"):], messages, temperature, meta)
    if model.startswith("groq:"):
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not set but model uses a groq: model")
        extra = {"reasoning_effort": GROQ_REASONING} if GROQ_REASONING else None
        return openai_chat(GROQ_URL, GROQ_API_KEY, model[len("groq:"):], messages, temperature, extra, meta)
    if model.startswith("claude:"):  # subscription auth via the Claude Code CLI
        return claude_cli_chat(model[len("claude:"):], messages, temperature, meta)
    if model.startswith("openai:"):
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set but model uses an openai: prefix")
        name = model[len("openai:"):]
        gpt5 = name.startswith("gpt-5")
        extra = {"reasoning_effort": OPENAI_REASONING} if OPENAI_REASONING else None
        return openai_chat(OPENAI_URL, OPENAI_API_KEY, name, messages, temperature, extra, meta,
                           token_param="max_completion_tokens" if gpt5 else "max_tokens",
                           send_temperature=not gpt5)
    if model.startswith("dashscope:"):
        if not DASHSCOPE_API_KEY:
            raise RuntimeError("DASHSCOPE_API_KEY not set but model uses a dashscope: prefix")
        extra = {"thinking_budget": DASHSCOPE_THINKING_BUDGET} if DASHSCOPE_THINKING_BUDGET else None
        return openai_chat(DASHSCOPE_URL, DASHSCOPE_API_KEY, model[len("dashscope:"):],
                           messages, temperature, extra, meta)
    if model.startswith("anthropic:"):
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set but model uses an anthropic: prefix")
        return anthropic_chat(model[len("anthropic:"):], messages, temperature, meta)
    if model.startswith("openrouter:"):
        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY not set but model uses an openrouter: prefix")
        prov = {"sort": "throughput"}
        if OPENROUTER_PROVIDERS:
            prov["only"] = OPENROUTER_PROVIDERS
        extra = {"provider": prov}
        if OPENROUTER_REASONING == "none":
            extra["reasoning"] = {"enabled": False}
        elif OPENROUTER_REASONING in ("low", "medium", "high"):
            extra["reasoning"] = {"effort": OPENROUTER_REASONING}
        elif OPENROUTER_REASONING.startswith("budget:"):
            extra["reasoning"] = {"max_tokens": int(OPENROUTER_REASONING.split(":", 1)[1])}
        return openai_chat(OPENROUTER_URL, OPENROUTER_API_KEY, model[len("openrouter:"):],
                           messages, temperature, extra, meta)
    return ollama_chat(model, messages, temperature=temperature, meta=meta)
