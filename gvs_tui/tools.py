"""Sandboxed file tools for the agent roles (B-full).

Every operation is confined to a single workspace directory. There is NO shell,
NO network, and NO code execution: the only capabilities are ``ls``,
``read_file``, ``write_file`` and ``append_file``, all resolved and checked to
stay under the workspace root, with size and call caps.

The tool protocol is model-agnostic: a tool call is a fenced ``tool`` block
holding one JSON object, so it works on any OpenAI-compatible endpoint.
"""

import os
import re
import json

# --- caps --------------------------------------------------------------------
MAX_WRITE_BYTES = 256 * 1024   # cap on a single write/append payload
MAX_READ_BYTES = 256 * 1024    # cap on how much read_file returns
MAX_TOOL_CALLS = 25            # cap on tool calls per role turn
MAX_LIST_ENTRIES = 200         # cap on ls entries returned


class ToolError(Exception):
    """Raised for a tool call that violates the sandbox."""


# --- path confinement --------------------------------------------------------

def safe_path(ws, rel):
    """Resolve ``rel`` under ``ws`` and ensure the result stays inside ``ws``.

    Returns the absolute path, or raises :class:`ToolError`.
    """
    if rel is None or not isinstance(rel, str) or not rel.strip():
        raise ToolError("empty path")
    rel = rel.strip()
    if rel.startswith("~"):
        raise ToolError("path must be relative to the workspace (no ~)")
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise ToolError("path must be relative to the workspace (no absolute paths)")
    ws_abs = os.path.abspath(ws)
    p_abs = os.path.abspath(os.path.normpath(os.path.join(ws_abs, rel)))
    if p_abs != ws_abs and not p_abs.startswith(ws_abs + os.sep):
        raise ToolError(f"path escapes the workspace: {rel!r}")
    return p_abs


def _list_files(ws):
    out = []
    for name in sorted(os.listdir(ws)):
        p = os.path.join(ws, name)
        if os.path.isfile(p):
            out.append({"name": name, "size": os.path.getsize(p)})
    return out[:MAX_LIST_ENTRIES]


def file_listing(ws):
    """A short human-readable listing of the workspace (for prompts)."""
    files = _list_files(ws)
    if not files:
        return "(workspace is empty)"
    return "\n".join(f"  {f['name']}  ({f['size']} bytes)" for f in files)


# --- tool execution ----------------------------------------------------------

def execute_tool(name, args, ws, read_only=False):
    """Execute one tool call, confined to ``ws``.

    Returns a human-readable result string (also used as the tool-result
    message fed back to the model). Raises :class:`ToolError` for a call that
    violates the sandbox; the caller turns that into an error result string.
    """
    args = args or {}
    if name == "ls":
        return file_listing(ws)
    if name == "read_file":
        p = safe_path(ws, args.get("path"))
        if not os.path.isfile(p):
            return f"ERROR: no such file: {args.get('path')}"
        with open(p, "rb") as f:
            raw = f.read(MAX_READ_BYTES + 1)
        truncated = len(raw) > MAX_READ_BYTES
        if truncated:
            raw = raw[:MAX_READ_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"ERROR: {args.get('path')} is not UTF-8 text"
        note = "\n[truncated: file is larger than the read cap]" if truncated else ""
        return text + note
    if read_only and name in ("write_file", "append_file"):
        raise ToolError(f"{name} is not allowed (read-only role)")
    if name == "write_file":
        p = safe_path(ws, args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("'content' must be a string")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError(
                f"content exceeds the {MAX_WRITE_BYTES} byte write cap; "
                "split it across multiple write_file/append_file calls")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return f"OK: wrote {len(content)} chars to {args.get('path')}"
    if name == "append_file":
        p = safe_path(ws, args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("'content' must be a string")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError(
                f"content exceeds the {MAX_WRITE_BYTES} byte cap; split it")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return f"OK: appended {len(content)} chars to {args.get('path')}"
    raise ToolError(f"unknown tool {name!r}")


def run_tool(name, args, ws, read_only=False):
    """Like :func:`execute_tool` but returns an error string instead of raising."""
    try:
        return execute_tool(name, args, ws, read_only=read_only)
    except ToolError as e:
        return f"ERROR: {e}"


# --- tool-call parsing -------------------------------------------------------

_TOOL_FENCE = re.compile(r"```tool[ \t]*\n([\s\S]*?)```")
_TOOLCALL_LINE = re.compile(r"^\s*TOOLCALL:\s*(\{.*\})\s*$", re.M)


def parse_tool_block(text):
    """Extract a tool call from a model response.

    Returns ``{"name": str, "args": dict}`` or ``None`` if the response has no
    valid tool call. Accepts a fenced ``tool`` block or a ``TOOLCALL: {...}``
    line.
    """
    if not text:
        return None
    m = _TOOL_FENCE.search(text)
    if m:
        payload = m.group(1)
    else:
        m2 = _TOOLCALL_LINE.search(text)
        if not m2:
            return None
        payload = m2.group(1)
    try:
        obj = json.loads(payload.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    args = obj.get("args", {})
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(args, dict):
        args = {}
    return {"name": name.strip(), "args": args}


# --- prompt text -------------------------------------------------------------

READ_ONLY_PROTOCOL = """
You have READ-ONLY file tools for the workspace (you cannot write or create files). To use
one, your ENTIRE response must be a single fenced code block tagged "tool" with one JSON
object, no other text:
```tool
{"name": "read_file", "args": {"path": "notes.md"}}
```
Available: {"name":"ls","args":{}}  and  {"name":"read_file","args":{"path":"..."}}.
Use them to inspect the files a worker produced before you decide. When you are done,
respond with your final sections and NO tool block.
"""

TOOL_PROTOCOL = """
You have FILE TOOLS for a shared workspace. To use one, your ENTIRE response must be a
single fenced code block tagged "tool" containing one JSON object, with no other text:

```tool
{"name": "write_file", "args": {"path": "brief.md", "content": "# Title\\n\\nBody text..."}}
```

Available tools:
- {"name":"ls","args":{}}                          list the files in the workspace
- {"name":"read_file","args":{"path":"notes.md"}}  read a file
- {"name":"write_file","args":{"path":"brief.md","content":"..."}}  create or overwrite a file
- {"name":"append_file","args":{"path":"notes.md","content":"..."}} append to a file

Rules:
- One tool call per response. Paths are relative to the workspace (no leading "/", no "..").
- ACTUALLY create the files with write_file/append_file -- do not merely describe files you
  "wrote". You may create several files.
- When you are finished and no more tool calls are needed, respond with your final sections
  and NO "tool" block.
"""
