"""Headless end-to-end test of the orchestration loop with the mock provider.

Run:  python tests/test_loop.py
"""

import os
import sys
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gvs_tui import ledger
from gvs_tui.profiles import get_profile
from gvs_tui.verifier import run_python_tests
from gvs_tui.loop import run_loop

WS_ROOT = os.path.join(os.path.dirname(__file__), "_ws")


def show(label, status):
    print(f"\n=== {label} ===")
    print(f"  ws            : {status['ws']}")
    print(f"  profile       : {status['profile']}  deliverable dir {status['artifact_dir']}/")
    print(f"  calls         : {status['n_calls']}   iters: {status['iters']}")
    print(f"  finish_reason : {status['finish_reason']}   truncated: {status['truncated_calls']}")
    print(f"  has_artifact  : {status['has_artifact']}   stopped: {status['stopped']}")
    art = ledger.out_brief(status["ws"])
    print(f"  deliverable ({len(art)} chars):")
    for line in art.splitlines()[:8]:
        print(f"    | {line}")


def main():
    shutil.rmtree(WS_ROOT, ignore_errors=True)
    os.makedirs(WS_ROOT, exist_ok=True)

    # 1) code profile + sample-test verifier (the full feedback loop)
    code_task = ("Read a single integer n from stdin and print n*n to stdout. "
                 "Input constraints: 1 <= n <= 10^9.")
    tests = [{"input": "3", "output": "9"}, {"input": "1", "output": "1"}]
    s1 = run_loop(code_task, get_profile("code"), "mock:gvs",
                  ws_root=WS_ROOT, max_iters=6,
                  verifier=run_python_tests, tests=tests,
                  log=lambda m: print(m))
    assert s1["has_artifact"], "code run produced no artifact"
    art = ledger.read(s1["ws"], "out/solution.py")
    assert "def main" in art, "mock code not written to out/solution.py"
    # the verifier must have actually run and passed (check transcript logs)
    show("code + verifier", s1)

    # 2) answer-style profile (math), no verifier
    math_task = "What is 6 * 7? Answer with the integer."
    s2 = run_loop(math_task, get_profile("math"), "mock:gvs",
                  ws_root=WS_ROOT, max_iters=6,
                  log=lambda m: print(m))
    assert s2["has_artifact"], "math run produced no artifact"
    assert "ANSWER:" in ledger.read(s2["ws"], "out/result.md"), "no ANSWER: marker in out/result.md"
    show("math (answer-style)", s2)

    # 3) general profile, no verifier
    gen_task = "Write a 3-sentence summary of why fresh-context agents with a shared " \
               "filesystem ledger can outperform a single long context."
    s3 = run_loop(gen_task, get_profile("general"), "mock:gvs",
                  ws_root=WS_ROOT, max_iters=6,
                  log=lambda m: print(m))
    assert s3["has_artifact"], "general run produced no artifact"
    show("general", s3)

    # 4) transcript sanity: every call recorded with a role
    recs = [r for r in ledger.read_transcript(s1["ws"]) if not r.get("_meta")]
    roles = [r["role"] for r in recs]
    print("\ntranscript roles (code run):", roles)
    assert "primary_plan" in roles and "ideation" in roles
    assert any(r.startswith("worker") for r in roles)
    assert "primary_manage" in roles

    # 5) ledger files all present (deliverable lives in out/)
    for name in ("task.md", "plan.md", "notes.md", "notes.log.md", "tasks.json",
                 "out", "transcript.jsonl"):
        assert os.path.exists(os.path.join(s1["ws"], name)), f"missing {name}"
    # the append-only history has at least one worker entry
    log = ledger.read(s1["ws"], "notes.log.md")
    assert "## worker:" in log, f"notes.log.md has no worker entry: {log!r}"
    tasks = ledger.load_tasks(s1["ws"])
    print("final task list (code run):")
    for t in tasks:
        print(f"  [{t['status']}] {t['desc']}")
    print("notes.log.md (code run):")
    for line in log.splitlines():
        print(f"    | {line}")

    print("\nALL LOOP TESTS PASSED")


if __name__ == "__main__":
    main()
