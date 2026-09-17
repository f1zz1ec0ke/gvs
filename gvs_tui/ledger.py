"""The shared filesystem ledger.

The ledger is the coordination medium of the whole paradigm: every role is a
fresh model context, so the only durable channel between them is a directory
of plain files.

    task.md          the task text (immutable once written)
    plan.md          the manager's overarching plan
    notes.md         shared notes, rewritten by workers (last writer wins)
    tasks.json       the manager's live task list
    <artifact>       the current solution/answer/report (profile-defined)
    transcript.jsonl one line per model call (request, response, usage)

Everything here is synchronous and side-effectful on disk, so the TUI can
simply re-read the ledger after each event -- the filesystem is the source of
truth.
"""

import os
import re
import json
import time
import hashlib

MAX_TASKS_DEFAULT = 12
MAX_PLAN_CHARS = 4000
MAX_ANSWER_CHARS = 20000


def slug(text):
    return hashlib.md5(text.encode()).hexdigest()[:12]


def _path(ws, name):
    return os.path.join(ws, name)


def read(ws, name):
    p = _path(ws, name)
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write(ws, name, content):
    with open(_path(ws, name), "w", encoding="utf-8") as f:
        f.write(content)


def append(ws, name, content):
    with open(_path(ws, name), "a", encoding="utf-8") as f:
        f.write(content)


def init_workspace(ws, artifact_file, task_text):
    """Create a fresh ledger for one task."""
    os.makedirs(ws, exist_ok=True)
    for name in ("task.md", "notes.md", "transcript.jsonl", "plan.md",
                 artifact_file, "tasks.json"):
        write(ws, name, "")
    write(ws, "task.md", task_text)


def record(ws, rec):
    """Append one transcript record (every model call lands here)."""
    with open(_path(ws, "transcript.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_transcript(ws):
    recs = []
    for line in read(ws, "transcript.jsonl").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn last line from a hard kill
    return recs


# --- reply parsing ----------------------------------------------------------

def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def strip_code(text):
    return re.sub(r"```.*?```", "[code omitted -- approach only]", text, flags=re.DOTALL).strip()


def sections(text):
    """Split a reply into {SECTION_NAME: body} on '### NAME' / '**NAME**:' / 'NAME:' headers."""
    out, cur, buf = {}, None, []
    for line in strip_think(text).splitlines():
        s = line.strip()
        m = (re.match(r"^#{1,3}\s*([A-Za-z_]+)\s*$", s)
             or re.match(r"^\*\*([A-Za-z_]+)\*\*\s*:?\s*$", s)
             or re.match(r"^([A-Z_]{3,})\s*:\s*$", s))
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).upper(), []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def bullets(text):
    items = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)", line)
        if m:
            items.append(m.group(1).strip())
    return items


def extract_fenced(text, fence="python"):
    m = re.search(r"```" + re.escape(fence) + r"\s*\n(.*?)```", strip_think(text), re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n(.*?)```", strip_think(text), re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_tasks(text, max_tasks=MAX_TASKS_DEFAULT):
    out = []
    for b in bullets(text):
        m = re.match(r"\[\s*([a-z_ ]+?)\s*\]\s*(.*)", b, re.I)
        if m:
            raw = m.group(1).lower().replace(" ", "").replace("_", "")
            desc = m.group(2).strip()
            status = "done" if raw == "done" else ("in_progress" if raw in ("wip", "inprogress") else "pending")
        else:
            desc, status = b, "pending"
        if desc:
            out.append({"id": len(out) + 1, "desc": desc, "status": status, "result": ""})
    return out[:max_tasks]


def has_artifact(ws, profile):
    """True if the ledger already holds a usable final artifact."""
    if profile.kind == "code":
        return bool(read(ws, profile.artifact_file).strip())
    text = read(ws, profile.artifact_file)
    if profile.answer_marker:
        return bool(re.search(profile.answer_marker, text, re.I))
    return bool(text.strip())


def load_tasks(ws):
    try:
        data = json.loads(read(ws, "tasks.json") or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def save_tasks(ws, tasks):
    write(ws, "tasks.json", json.dumps(tasks, indent=2))


def usage_totals(ws):
    """Aggregate prompt/completion tokens across the transcript."""
    prompt = completion = 0
    calls = 0
    for r in read_transcript(ws):
        if r.get("_meta"):
            continue
        calls += 1
        prompt += int(r.get("prompt_tokens") or 0)
        completion += int(r.get("completion_tokens") or 0)
    return {"calls": calls, "prompt_tokens": prompt, "completion_tokens": completion}


# --- manager chat (user <-> manager dialogue, stored in the ledger) --------

def read_chat(ws):
    recs = []
    for line in read(ws, "chat.jsonl").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def append_chat(ws, role, text, reasoning=""):
    rec = {"role": role, "text": text, "t": now()}
    if reasoning:
        rec["reasoning"] = reasoning
    with open(_path(ws, "chat.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent_chat(ws, n=10):
    """The last ``n`` chat lines, formatted as 'ROLE: text' for a prompt."""
    lines = []
    for rec in read_chat(ws)[-n:]:
        text = (rec.get("text") or "").strip()
        if text:
            lines.append(f"{str(rec.get('role', '?')).upper()}: {text}")
    return "\n".join(lines)


# --- saved runs -------------------------------------------------------------

_ARTIFACT_FILES = ("solution.py", "answer.md", "report.md", "result.md")


def run_summary(ws):
    task = read(ws, "task.md").strip()
    recs = [r for r in read_transcript(ws) if not r.get("_meta")]
    metas = [r for r in read_transcript(ws) if r.get("_meta")]
    m0 = metas[0] if metas else {}
    mlast = metas[-1] if metas else {}
    has_artifact = any(read(ws, f).strip() for f in _ARTIFACT_FILES)
    try:
        updated = os.path.getmtime(ws)
    except OSError:
        updated = None
    return {
        "ws": ws,
        "slug": os.path.basename(ws),
        "task": task[:200],
        "n_calls": len(recs),
        "model": m0.get("model"),
        "profile": m0.get("profile"),
        "continued": bool(mlast.get("continued", False)),
        "updated": updated,
        "has_artifact": has_artifact,
    }


def list_runs(ws_root):
    runs = []
    if not os.path.isdir(ws_root):
        return runs
    for name in sorted(os.listdir(ws_root), reverse=True):
        ws = os.path.join(ws_root, name)
        if not os.path.isdir(ws):
            continue
        s = run_summary(ws)
        if s["n_calls"] > 0 or s["task"]:
            runs.append(s)
    return runs


def now():
    return time.time()
