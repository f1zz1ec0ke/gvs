"""Optional verifier hooks.

A verifier is ground-truth feedback for the manager: after a worker writes the
artifact, the verifier checks it and the result is fed back as a SAMPLE-TESTS
verdict. This is what lets the manager mark a task "done" only when the
artifact actually works, and switch approach when it keeps failing.

A verifier is any callable ``verifier(ws, profile, tests) -> dict | None``
returning ``{"ran": bool, "passed": int, "total": int, "fail": {...} | None}``
(or ``None`` to skip). ``tests`` is an opaque object the verifier understands
(e.g. a list of {"input": ..., "output": ...} sample cases).

The default Python verifier runs the generated code in a SANDBOXED subprocess:
an isolated temp working directory (the code cannot clobber the workspace or
project), a per-test timeout plus a total cap, a scrubbed environment (no API
keys or secrets), and a best-effort network block (dead HTTP/HTTPS proxy).
"""

import os
import sys
import time
import shutil
import subprocess
import tempfile

# --- sandbox knobs -----------------------------------------------------------
SANDBOX_TIMEOUT = 10          # per-test timeout (seconds)
SANDBOX_TOTAL_TIMEOUT = 120   # total cap across all tests (seconds)
_DEAD_PROXY = "http://127.0.0.1:9"  # discard port; nothing listens -> outbound HTTP fails


def _scrubbed_env():
    """A minimal environment for the sandboxed subprocess.

    Built from scratch (NOT copied from ``os.environ``) so API keys, tokens and
    other secrets are excluded. A dead proxy is set to block outbound HTTP as a
    best effort (proxy-aware clients such as urllib/requests will fail to reach
    the internet).
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
        "HTTP_PROXY": _DEAD_PROXY,
        "HTTPS_PROXY": _DEAD_PROXY,
        "http_proxy": _DEAD_PROXY,
        "https_proxy": _DEAD_PROXY,
        "NO_PROXY": "",
    }


def run_python_tests(ws, profile, tests, timeout=SANDBOX_TIMEOUT):
    """Default verifier: run the deliverable (the ``out/`` directory) against
    stdin/stdout sample tests, in a sandboxed subprocess.

    ``tests`` is a list of dicts with ``"input"`` and ``"output"`` keys (and
    optionally ``"testtype"`` containing "stdin"). ``profile`` is accepted for a
    uniform verifier signature; the deliverable is always ``out/``.
    """
    out_dir = os.path.join(ws, "out")
    program = _find_program(out_dir)
    stdin_tests = [t for t in (tests or [])
                   if "stdin" in str(t.get("testtype", "")).lower()]
    if not stdin_tests:
        stdin_tests = [t for t in (tests or []) if "input" in t]
    if not program or not stdin_tests:
        return {"ran": False}

    # Isolated temp working dir: copy the WHOLE out/ deliverable in (so multi-file
    # programs with imports work) and run there, so the code cannot touch the
    # workspace or the project.
    sandbox = tempfile.mkdtemp(prefix="gvs_sandbox_")
    out_copy = os.path.join(sandbox, "out")
    shutil.copytree(out_dir, out_copy)
    sol = os.path.join(out_copy, os.path.relpath(program, out_dir))
    env = _scrubbed_env()

    passed, fail = 0, None
    total_deadline = time.time() + SANDBOX_TOTAL_TIMEOUT
    try:
        for t in stdin_tests:
            if time.time() > total_deadline:
                fail = fail or {"input": (t.get("input", "") or "")[:600],
                                "expected": ((t.get("output", "") or "").strip())[:400],
                                "got": "<total sandbox time limit exceeded>"}
                break
            inp = t.get("input", "") or ""
            exp = (t.get("output", "") or "").strip()
            try:
                r = subprocess.run([sys.executable, sol], input=inp, capture_output=True,
                                   text=True, timeout=timeout,
                                   cwd=os.path.dirname(sol), env=env)
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
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return {"ran": True, "passed": passed, "total": len(stdin_tests), "fail": fail}


def _find_program(out_dir):
    """Find the entry-point program in the deliverable directory.

    Prefers ``main.py``, then ``solution.py``, then the single ``.py`` file, then
    the first ``.py`` (sorted). Returns an absolute path or ``None``.
    """
    if not os.path.isdir(out_dir):
        return None
    for name in ("main.py", "solution.py"):
        p = os.path.join(out_dir, name)
        if os.path.isfile(p):
            return p
    pys = []
    for root, _dirs, names in os.walk(out_dir):
        for name in names:
            if name.endswith(".py"):
                pys.append(os.path.join(root, name))
    if not pys:
        return None
    if len(pys) == 1:
        return pys[0]
    top = [p for p in pys if os.path.dirname(p) == out_dir]
    if len(top) == 1:
        return top[0]
    return sorted(pys)[0]


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
