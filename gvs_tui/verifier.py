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
    """Default verifier for the ``code`` profile: run the artifact against
    stdin/stdout sample tests, in a sandboxed subprocess.

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

    # Isolated temp working dir: the generated code cannot touch the workspace
    # or the project (relative writes land in this throwaway directory).
    sandbox = tempfile.mkdtemp(prefix="gvs_sandbox_")
    sol = os.path.join(sandbox, profile.artifact_file)
    with open(sol, "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
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
                                   text=True, timeout=timeout, cwd=sandbox, env=env)
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
