"""End-to-end test of the researcher role and the [research] dispatch.

A mock model is installed: the manager marks a [research] task, the researcher
issues a search tool call then writes research.md, and a worker uses the
research to write the deliverable. This verifies the [research] routing, the
researcher's web tools, and that research.md lands in the workspace.

Run:  python tests/test_researcher.py
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


class FakeSearch:
    def search(self, q):
        return [{"title": "Source 1", "url": "https://s1",
                 "snippet": "a documented fact about " + q}]


def make_mock():
    counts = {"manage": 0, "researcher": 0, "worker": 0}

    def mock(model, messages, temperature=None, meta=None):
        if meta is not None:
            meta["provider"] = "mock:research"
            meta["finish_reason"] = "stop"
        sysmsg = messages[0]["content"]
        if "RESEARCHER subagent" in sysmsg:
            counts["researcher"] += 1
            n = counts["researcher"]
            if n == 1:
                return '```tool\n{"name": "search", "args": {"query": "the topic"}}\n```'
            if n == 2:
                return ('```tool\n{"name": "write_file", "args": {"path": "research.md", '
                        '"content": "# Research\\n\\nKey fact: the topic is well-documented '
                        '(https://s1)."}}\n```')
            return ("### STATUS\nsolved\n### NEXT\nnone\n"
                    "### NOTES\n- Researched the topic; key fact recorded.")
        if "WORKER subagent" in sysmsg:
            counts["worker"] += 1
            if counts["worker"] == 1:
                return ('```tool\n{"name": "write_file", "args": {"path": "out/result.md", '
                        '"content": "# Deliverable\\n\\nBased on the research, the answer is X."}}\n```')
            return ("### STATUS\nsolved\n### NEXT\nnone\n"
                    "### NOTES\n- Used the research to write the deliverable.")
        if "FIRST WORKER" in sysmsg:
            return "### NOTES\n- A\n### NEXT\n- Try A"
        if "overarching plan" in sysmsg:
            return "### PLAN\nPlan.\n### TASKS\n- [todo] Research the topic"
        if "CUT OFF" in sysmsg:
            return "Partway through."
        # primary_manage
        counts["manage"] += 1
        n = counts["manage"]
        if n == 1:
            return ("### STATUS\ncontinue\n### NEXT\n[research] Find facts about the topic\n"
                    "### TASKS\n- [todo] Research the topic\n### UPDATE\nNeed research first.")
        if n == 2:
            return ("### STATUS\ncontinue\n### NEXT\nWrite the deliverable using the research\n"
                    "### TASKS\n- [done] Research the topic\n- [todo] Write the deliverable\n"
                    "### UPDATE\nResearch done; now produce the deliverable.")
        return ("### STATUS\ndone\n### NEXT\nnone\n"
                "### TASKS\n- [done] Research the topic\n- [done] Write the deliverable\n"
                "### UPDATE\nDone.")
    return mock


def main():
    ws_root = tempfile.mkdtemp(prefix="gvs_researcher_")
    orig = loop_mod.chat_fn
    loop_mod.chat_fn = make_mock()
    try:
        profile = get_profile("general")
        status = loop_mod.run_loop(
            "What is the topic? Write a brief about it.", profile, "mock:research",
            ws_root=ws_root, max_iters=5, search=FakeSearch(), log=lambda m: print(m))
        ws = status["ws"]
        research = ledger.read(ws, "research.md")
        art = ledger.read(ws, "out/result.md")
        check("research.md was written", "Key fact" in research)
        check("research.md cites a source", "https://s1" in research)
        check("deliverable was written (worker used the research)", "Based on the research" in art)

        recs = [r for r in ledger.read_transcript(ws) if not r.get("_meta")]
        roles = [r.get("role") for r in recs]
        check("a researcher role was dispatched",
              any(r and r.startswith("researcher") for r in roles))
        researcher_search = [r for r in recs
                             if str(r.get("role", "")).startswith("researcher")
                             and "search" in str(r.get("response", ""))]
        check("researcher made a search tool call", len(researcher_search) >= 1)
        print("\nresearch.md head:")
        for line in research.splitlines()[:5]:
            print("   |", line)
    finally:
        loop_mod.chat_fn = orig

    if failures:
        print(f"\n{failures} RESEARCHER TEST(S) FAILED")
        sys.exit(1)
    print("\nALL RESEARCHER TESTS PASSED")


if __name__ == "__main__":
    main()
