"""Optional verifier hooks.

A verifier is ground-truth feedback for the manager: after a worker writes the
artifact, the verifier checks it and the result is fed back as a SAMPLE-TESTS
verdict. This is what lets the manager mark a task "done" only when the
artifact actually works, and switch approach when it keeps failing.

A verifier is any callable ``verifier(ws, profile, tests) -> dict | None``
returning ``{"ran": bool, "passed": int, "total": int, "fail": {...} | None}``
(or ``None`` to skip). ``tests`` is an opaque object the verifier understands
(e.g. a list of {"input": ..., "output": ...} sample cases).
"""

import os
import sys
import subprocess


def run_python_tests(ws, profile, tests, timeout=10):
    """Default verifier for the ``code`` profile: run the artifact against
    stdin/stdout sample tests.

    ``tests`` is a list of dicts with ``"input"`` and ``"output"`` keys (and
    optionally ``"testtype"`` containing "stdin"). Mirrors GVS5H's sample-test
    execution. ``profile`` is accepted for a uniform verifier signature.
    """
    code = _read_artifact(ws, profile.artifact_file)
    stdin_tests = [t for t in (tests or [])
                   if "stdin" in str(t.get("testtype", "")).lower()]
    if not stdin_tests:
        stdin_tests = [t for t in (tests or []) if "input" in t]
    if not code.strip() or not stdin_tests:
        return {"ran": False}
    sol = os.path.join(ws, profile.artifact_file)
    passed, fail = 0, None
    for t in stdin_tests:
        inp = t.get("input", "") or ""
        exp = (t.get("output", "") or "").strip()
        try:
            r = subprocess.run([sys.executable, sol], input=inp, capture_output=True,
                               text=True, timeout=timeout)
            got = (r.stdout or "").strip()
            if r.returncode != 0 and not got:
                got = f"<runtime error: {(r.stderr or '')[:200]}>"
        except subprocess.TimeoutExpired:
            got = f"<timed out (> {timeout}s)>"
        except Exception as e:  # noqa: BLE001
            got = f"<error: {e}>"
        if got == exp:
            passed += 1
        elif fail is None:
            fail = {"input": inp[:600], "expected": exp[:400], "got": got[:400]}
    return {"ran": True, "passed": passed, "total": len(stdin_tests), "fail": fail}


def _read_artifact(ws, name):
    try:
        with open(os.path.join(ws, name), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def sample_feedback(res):
    """Render a verifier result as the ground-truth line the manager sees."""
    if not res or not res.get("ran"):
        return ""
    if res["passed"] == res["total"]:
        return (f"[SAMPLE TESTS: PASSED all {res['total']} public samples -- the solution "
                "looks correct.] ")
    f = res.get("fail") or {}
    return ("[SAMPLE TESTS: FAILED -- passed {p}/{t}. The current solution is WRONG. First "
            "failing case: input={i!r} expected={e!r} got={g!r}. Fix the bug or, if this "
            "approach keeps failing, switch to a DIFFERENT approach.] ").format(
                p=res["passed"], t=res["total"], i=f.get("input", ""),
                e=f.get("expected", ""), g=f.get("got", ""))
