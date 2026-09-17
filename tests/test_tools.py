"""Unit tests for the sandboxed file tool layer.

Run:  python tests/test_tools.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gvs_tui import tools

failures = 0


def check(name, cond):
    global failures
    if cond:
        print("  ok  " + name)
    else:
        print("  FAIL " + name)
        failures += 1


def main():
    ws = tempfile.mkdtemp(prefix="gvs_tools_")

    print("safe_path")
    check("relative stays inside",
          tools.safe_path(ws, "brief.md") == os.path.abspath(os.path.join(ws, "brief.md")))
    check("nested subdir stays inside",
          tools.safe_path(ws, "sub/dir/x.md").startswith(os.path.abspath(ws) + os.sep))
    for bad in ("../escape.md", "a/../../escape.md", "/etc/passwd", "C:\\Windows\\x",
                "~/home/x", "..", ""):
        try:
            tools.safe_path(ws, bad)
            check(f"rejects {bad!r}", False)
        except tools.ToolError:
            check(f"rejects {bad!r}", True)

    print("write_file / read_file / append_file / ls")
    r = tools.execute_tool("write_file", {"path": "brief.md", "content": "# Hello\nworld"}, ws)
    check("write returns OK", r.startswith("OK: wrote"))
    check("file exists on disk", os.path.isfile(os.path.join(ws, "brief.md")))
    r = tools.execute_tool("read_file", {"path": "brief.md"}, ws)
    check("read returns content", "# Hello\nworld" in r)
    r = tools.execute_tool("append_file", {"path": "brief.md", "content": "\nmore"}, ws)
    check("append returns OK", r.startswith("OK: appended"))
    r = tools.execute_tool("read_file", {"path": "brief.md"}, ws)
    check("append landed", "more" in r)
    r = tools.execute_tool("ls", {}, ws)
    check("ls lists the file", "brief.md" in r)
    r = tools.execute_tool("read_file", {"path": "nope.md"}, ws)
    check("read missing -> ERROR", r.startswith("ERROR"))

    print("confinement + caps + read-only")
    try:
        tools.execute_tool("write_file", {"path": "../escape.md", "content": "x"}, ws)
        check("write cannot escape ws", False)
    except tools.ToolError:
        check("write cannot escape ws", True)
    check("escape file NOT created", not os.path.exists(os.path.join(os.path.dirname(ws), "escape.md")))
    big = "a" * (tools.MAX_WRITE_BYTES + 1)
    try:
        tools.execute_tool("write_file", {"path": "big.md", "content": big}, ws)
        check("write over cap rejected", False)
    except tools.ToolError:
        check("write over cap rejected", True)
    try:
        tools.execute_tool("write_file", {"path": "ro.md", "content": "x"}, ws, read_only=True)
        check("read-only blocks write", False)
    except tools.ToolError:
        check("read-only blocks write", True)
    r = tools.execute_tool("ls", {}, ws, read_only=True)
    check("read-only allows ls", "brief.md" in r)
    r = tools.run_tool("write_file", {"path": "../x.md", "content": "x"}, ws)
    check("run_tool returns error string", r.startswith("ERROR"))
    r = tools.run_tool("bogus", {}, ws)
    check("run_tool unknown tool -> error", r.startswith("ERROR"))

    print("parse_tool_block")
    good = '```tool\n{"name": "write_file", "args": {"path": "a.md", "content": "hi"}}\n```'
    p = tools.parse_tool_block(good)
    check("parses fenced tool block", p == {"name": "write_file", "args": {"path": "a.md", "content": "hi"}})
    good2 = 'thinking...\nTOOLCALL: {"name": "ls", "args": {}}\n'
    p = tools.parse_tool_block(good2)
    check("parses TOOLCALL line", p == {"name": "ls", "args": {}})
    check("no tool -> None", tools.parse_tool_block("just a normal answer ### STATUS\ndone") is None)
    check("bad json -> None", tools.parse_tool_block('```tool\n{not json}\n```') is None)
    check("empty -> None", tools.parse_tool_block("") is None)
    p = tools.parse_tool_block('```tool\n{"name": "read_file", "args": {"path": "notes.md"}}\n```')
    check("args captured", p and p["args"].get("path") == "notes.md")

    print("web tools (search / fetch)")
    class FakeSP:
        def search(self, q):
            return [{"title": "R1", "url": "https://r1", "snippet": "snip " + q}]
    sp = FakeSP()
    r = tools.run_tool("search", {"query": "asyncio"}, ws, web=True, search_provider=sp)
    check("search returns results", "R1" in r and "https://r1" in r and "snip asyncio" in r)
    r = tools.run_tool("search", {"query": "x"}, ws, web=False)
    check("search blocked for non-web role", r.startswith("ERROR"))
    r = tools.run_tool("search", {"query": "x"}, ws, web=True, search_provider=None)
    check("search with no provider -> error msg", "no search provider" in r)
    r = tools.run_tool("search", {}, ws, web=True, search_provider=sp)
    check("search requires a query", r.startswith("ERROR"))

    import urllib.request as _urlreq
    orig_urlopen = _urlreq.urlopen

    class _FakeResp:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        def __init__(self, data):
            self._d = data
        def read(self, n=-1):
            return self._d if n < 0 else self._d[:n]
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    _urlreq.urlopen = lambda req, timeout=None: _FakeResp(b"hello fetched page")
    try:
        r = tools.run_tool("fetch", {"url": "https://example.com"}, ws, web=True)
        # The result is a short pointer (the page is saved to a file, not inlined).
        check("fetch returns a pointer (not the full page)",
              "saved to research/web/" in r and "hello fetched page" in r)
        # The page was actually written to the workspace (research/web/).
        webdir = os.path.join(ws, "research", "web")
        saved = os.listdir(webdir) if os.path.isdir(webdir) else []
        check("fetch saved the page to the workspace", len(saved) == 1)
        if saved:
            with open(os.path.join(webdir, saved[0]), "rb") as f:
                check("saved page has the content", f.read() == b"hello fetched page")
        r = tools.run_tool("fetch", {"url": "https://example.com"}, ws, web=False)
        check("fetch blocked for non-web role", r.startswith("ERROR"))
        r = tools.run_tool("fetch", {"url": "file:///etc/passwd"}, ws, web=True)
        check("fetch rejects non-http url", r.startswith("ERROR"))
    finally:
        _urlreq.urlopen = orig_urlopen

    print()
    if failures:
        print(f"{failures} TOOL TEST(S) FAILED")
        sys.exit(1)
    print("ALL TOOL TESTS PASSED")


if __name__ == "__main__":
    main()
