"""Sandboxed tools for the agent roles (B-full + researcher).

File tools: every operation is confined to a single workspace directory. There
is NO shell and NO code execution: the file capabilities are ``ls``,
``read_file``, ``write_file`` and ``append_file``, all resolved and checked to
stay under the workspace root, with size and call caps.

Web tools: ``search`` and ``fetch`` are available ONLY to the researcher role
(``web=True``). ``fetch`` is http/https-only, size-capped and timed out;
``search`` goes through a user-configured SearchProvider.

The tool protocol is model-agnostic: a tool call is a fenced ``tool`` block
holding one JSON object, so it works on any OpenAI-compatible endpoint.
"""

import os
import re
import json
import gzip
import hashlib
import urllib.request
import urllib.error

# --- caps --------------------------------------------------------------------
MAX_WRITE_BYTES = 256 * 1024   # cap on a single write/append payload
MAX_READ_BYTES = 256 * 1024    # cap on how much read_file returns
MAX_TOOL_CALLS = 25            # cap on tool calls per role turn
MAX_LIST_ENTRIES = 200         # cap on ls entries returned
MAX_FETCH_BYTES = 512 * 1024   # cap on how much fetch returns
FETCH_TIMEOUT = 30             # seconds for a fetch


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


def _slug_url(url):
    """A stable, filesystem-safe filename for a fetched URL (hash + readable path)."""
    h = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:10]
    tail = str(url).split("://", 1)[-1]
    tail = re.sub(r"[^A-Za-z0-9._-]", "_", tail)[:60] or "page"
    return f"{h}_{tail}"


# --- tool execution ----------------------------------------------------------

def execute_tool(name, args, ws, read_only=False, web=False, search_provider=None):
    """Execute one tool call, confined to ``ws``.

    Returns a human-readable result string (also used as the tool-result
    message fed back to the model). Raises :class:`ToolError` for a call that
    violates the sandbox; the caller turns that into an error result string.

    ``web`` enables the ``search``/``fetch`` tools (researcher only);
    ``search_provider`` is the configured SearchProvider for ``search``.
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
        if not text.strip():
            return "(empty file)"
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
    if name == "search":
        if not web:
            raise ToolError("search is not available to this role")
        if search_provider is None:
            return ("ERROR: no search provider is configured, so you cannot search the web. "
                    "Do NOT retry search. Rely on your own knowledge for this and note in your "
                    "findings that you could not verify it externally.")
        query = args.get("query")
        if not query or not str(query).strip():
            raise ToolError("search requires a 'query'")
        try:
            results = search_provider.search(str(query).strip())
        except Exception as e:  # noqa: BLE001 - surfaced to the model
            return f"ERROR: search failed: {e}"
        from . import search as _search
        return _search.format_results(results)
    if name == "fetch":
        if not web:
            raise ToolError("fetch is not available to this role")
        url = args.get("url")
        if not url or not str(url).startswith(("http://", "https://")):
            raise ToolError("fetch requires an http(s) 'url'")
        try:
            req = urllib.request.Request(str(url), headers={"User-Agent": "gvs-tui/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                raw = r.read(MAX_FETCH_BYTES + 1)
                ctype = (r.headers.get("Content-Type") or "").lower()
                cenc = (r.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            return f"ERROR: fetch failed ({e.code} {e.reason}) for {url}"
        except Exception as e:  # noqa: BLE001 - surfaced to the model
            return f"ERROR: fetch failed for {url}: {e}"
        # Decompress gzip responses (some servers compress by default).
        if "gzip" in cenc or raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except Exception:  # noqa: BLE001 - keep the raw bytes if decompression fails
                pass
        truncated = len(raw) > MAX_FETCH_BYTES
        if truncated:
            raw = raw[:MAX_FETCH_BYTES]
        # Save the page to the workspace so the raw bytes do NOT bloat the model's
        # context. The researcher reads it with read_file when it needs the content.
        relpath = f"research/web/{_slug_url(url)}"
        p = safe_path(ws, relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(raw)
        is_text = any(k in ctype for k in ("text", "html", "json", "xml", "javascript"))
        try:
            preview = " ".join(raw[:1500].decode("utf-8", errors="replace").split())[:300]
        except Exception:
            preview = ""
        kind = (ctype or "unknown type") if not is_text else "text"
        trunc_note = " (truncated at the fetch cap)" if truncated else ""
        return (f"OK: fetched {url} ({len(raw)} bytes, {kind}{trunc_note}) -> saved to "
                f"{relpath}. The full page is NOT in this result; read it with read_file "
                f"when you need the content. Preview: {preview}")
    raise ToolError(f"unknown tool {name!r}")


def run_tool(name, args, ws, read_only=False, web=False, search_provider=None):
    """Like :func:`execute_tool` but returns an error string instead of raising."""
    try:
        return execute_tool(name, args, ws, read_only=read_only, web=web,
                            search_provider=search_provider)
    except ToolError as e:
        return f"ERROR: {e}"


# --- tool-call parsing -------------------------------------------------------

_TOOL_FENCE = re.compile(r"```tool[ \t]*\n")
_TOOLCALL_LINE = re.compile(r"^\s*TOOLCALL:\s*", re.M)
# Trailing XML tool-call tags some models append instead of a closing fence
# (e.g. "</parameter>\n</function>" or "</invoke>"). Whitespace between tags allowed.
_TRAILING_TAGS = re.compile(r"\s*(?:</?[A-Za-z_][\w.-]*[^>]*>\s*)+\Z", re.S)


def _first_json_object(text):
    """Parse the first JSON object in ``text`` with a tolerant decoder.

    Ignores trailing garbage (stray XML tags, the closing fence, prose) and
    recovers a dropped final ``}`` (some models close the block with an XML tag
    and omit the last brace). Returns a ``dict`` or ``None``.
    """
    brace = text.find("{")
    if brace == -1:
        return None
    cand = text[brace:]
    # light repairs: as-is, with trailing XML tags stripped, and with 1-3 extra '}'
    bases = [cand, _TRAILING_TAGS.sub("", cand).rstrip()]
    for base in bases:
        for k in range(4):
            try:
                obj, _end = json.JSONDecoder().raw_decode(base + "}" * k)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                return obj
    return None


def parse_tool_block(text):
    """Extract a tool call from a model response.

    Returns ``{"name": str, "args": dict}`` or ``None`` if the response has no
    valid tool call. Accepts a fenced ``tool`` block or a ``TOOLCALL: {...}``
    line. The JSON object is decoded tolerantly: it ignores trailing garbage
    (e.g. stray ``</parameter>``/``</invoke>`` tags some models emit instead of a
    closing fence) and recovers a dropped final ``}``.
    """
    if not text:
        return None
    m = _TOOL_FENCE.search(text)
    if m:
        start = m.end()
    else:
        m2 = _TOOLCALL_LINE.search(text)
        if not m2:
            return None
        start = m2.end()
    obj = _first_json_object(text[start:])
    if obj is None:
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
Close the block with a ``` fence (no XML tags such as </parameter> or </invoke>). Use them
to inspect the files a worker produced before you decide. When you are done, respond with
your final sections and NO tool block.
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
- The block is EXACTLY one JSON object between the opening ```tool fence and a closing ```
  fence. Close it with a ``` fence -- do NOT emit XML tags (no </parameter>, </function>, or
  </invoke>), and do not omit the final closing brace of the JSON object.
- ACTUALLY create the files with write_file/append_file -- do not merely describe files you
  "wrote". You may create several files.
- When you are finished and no more tool calls are needed, respond with your final sections
  and NO "tool" block.
"""

WEB_PROTOCOL = """
You also have WEB TOOLS for research. To use one, your ENTIRE response must be a single
fenced code block tagged "tool" with one JSON object, no other text. Close the block with a
``` fence (no XML tags such as </parameter> or </invoke>):
```tool
{"name": "search", "args": {"query": "python asyncio tutorial"}}
```
Available web tools:
- {"name":"search","args":{"query":"..."}}    web search (returns titled results with URLs + snippets)
- {"name":"fetch","args":{"url":"https://..."}}  fetch a page; it is SAVED to research/web/<file>
  and the result is only a short pointer + preview (the full page is NOT in the result).
Workflow: use search to find sources, fetch the most relevant ones, then read_file the saved
pages (research/web/...) to read the actual content. Fetch each URL at most once. Cite
sources by URL in your findings. If a search or fetch fails, try a different query or source
rather than giving up -- but do NOT re-fetch a page you already have.
"""
