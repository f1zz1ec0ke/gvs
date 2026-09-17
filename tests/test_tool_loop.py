"""End-to-end test of the B-full tool loop.

A mock model is installed that answers every role normally EXCEPT the worker,
which emits a ``write_file`` tool call on its first call and the final sections
on its second. This verifies that the real ``chat_tools`` closure in
``run_loop`` executes the tool, writes the file to the workspace, feeds the
result back, and that ``_worker`` detects the written artifact.

Run:  python tests/test_tool_loop.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import gvs_tui.loop as loop_mod
from gvs_tui import ledger
from gvs_tui.profiles import get_profile

failures = 0


def check(name, cond):
    global failures
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def make_tool_mock():
    """A chat_fn replacement. Roles answer normally; the WORKER emits a
    write_file tool call on its first call, then the final sections."""
    state = {"worker_calls": 0}

    def mock(model, messages, temperature=None, meta=None):
        if meta is not None:
            meta["provider"] = "mock:tool"
            meta["finish_reason"] = "stop"
            meta["prompt_tokens"] = 10
            meta["completion_tokens"] = 10
        sysmsg = messages[0]["content"]
        if "WORKER subagent" in sysmsg:
            state["worker_calls"] += 1
            if state["worker_calls"] == 1:
                return ('```tool\n'
                        '{"name": "write_file", "args": {"path": "result.md", '
                        '"content": "# Real Deliverable\\n\\nThis was written by a tool call."}}\n'
                        '```')
            return ("### STATUS\nsolved\n### NEXT\nnone\n"
                    "### NOTES\n- Wrote the deliverable via write_file.")
        if "FIRST WORKER" in sysmsg:
            return "### NOTES\n- Approach A\n- Approach B\n### NEXT\n- Try approach A"
        if "overarching plan" in sysmsg:
            return "### PLAN\nA short plan.\n### TASKS\n- [todo] Produce the deliverable"
        if "CUT OFF" in sysmsg:
            return "It got partway through."
        # primary_manage
        return ("### STATUS\ndone\n### NEXT\nnone\n"
                "### TASKS\n- [done] Produce the deliverable\n### UPDATE\nDone.")
    return mock


def main():
    ws_root = tempfile.mkdtemp(prefix="gvs_toolloop_")
    orig = loop_mod.chat_fn
    loop_mod.chat_fn = make_tool_mock()
    try:
        profile = get_profile("general")
        status = loop_mod.run_loop(
            "Write a one-paragraph deliverable about testing.", profile, "mock:tool",
            ws_root=ws_root, max_iters=3, log=lambda m: print(m))
        ws = status["ws"]
        art = ledger.read(ws, profile.artifact_file)
        check("worker wrote the artifact via a tool call", "Real Deliverable" in art)
        check("artifact is the tool-written content", "tool call" in art)

        recs = [r for r in ledger.read_transcript(ws) if not r.get("_meta")]
        tool_recs = [r for r in recs
                     if "worker" in str(r.get("role", "")).lower()
                     and "```tool" in str(r.get("response", ""))]
        check("transcript records the worker tool-call step", len(tool_recs) >= 1)
        fed_back = [r for r in recs if "TOOL RESULT" in str(r.get("request", ""))]
        check("tool result was fed back to the model", len(fed_back) >= 1)

        # the file must exist on disk in the workspace
        check("result.md exists on disk",
              os.path.isfile(os.path.join(ws, "result.md")))
        print("\nartifact:")
        for line in art.splitlines()[:6]:
            print("   |", line)
    finally:
        loop_mod.chat_fn = orig

    if failures:
        print(f"\n{failures} TOOL-LOOP TEST(S) FAILED")
        sys.exit(1)
    print("\nALL TOOL-LOOP TESTS PASSED")


if __name__ == "__main__":
    main()
