"""Tests for the sandboxed Python verifier.

Run:  python tests/test_verifier_sandbox.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gvs_tui.profiles import get_profile
from gvs_tui import verifier

failures = 0


def check(name, cond):
    global failures
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def ws_with(code):
    ws = tempfile.mkdtemp(prefix="gvs_verifier_")
    os.makedirs(os.path.join(ws, "out"))
    with open(os.path.join(ws, "out", "solution.py"), "w", encoding="utf-8") as f:
        f.write(code)
    return ws, get_profile("code")


def main():
    prof = get_profile("code")

    print("normal pass/fail")
    ws, _ = ws_with("import sys\nn = int(sys.stdin.read())\nprint(n*n)\n")
    tests = [{"input": "3", "output": "9"}, {"input": "1", "output": "1"}]
    res = verifier.run_python_tests(ws, prof, tests)
    check("passes correct samples", res["ran"] and res["passed"] == 2 and res["total"] == 2)
    ws, _ = ws_with("import sys\nn = int(sys.stdin.read())\nprint(n+1)\n")
    res = verifier.run_python_tests(ws, prof, tests)
    check("fails a wrong sample", res["ran"] and res["passed"] < res["total"] and res["fail"])

    print("isolated temp dir (code cannot clobber the workspace)")
    ws, _ = ws_with("open('leak.txt','w').write('x')\nprint('ok')\n")
    res = verifier.run_python_tests(ws, prof, [{"input": "", "output": "ok"}])
    check("code ran", res["ran"] and res["passed"] == 1)
    check("leak.txt NOT in the workspace", not os.path.exists(os.path.join(ws, "leak.txt")))

    print("hanging script times out")
    ws, _ = ws_with("import time\ntime.sleep(30)\n")
    res = verifier.run_python_tests(ws, prof, [{"input": "", "output": "x"}], timeout=2)
    check("timed out", res["ran"] and res["fail"] and "timed out" in res["fail"]["got"])

    print("env secrets are not leaked to the sandbox")
    os.environ["GVS_TEST_SECRET"] = "secret123"
    ws, _ = ws_with("import os\nprint(os.environ.get('GVS_TEST_SECRET', 'NOTSET'))\n")
    res = verifier.run_python_tests(ws, prof, [{"input": "", "output": "NOTSET"}])
    check("secret not visible to the code", res["ran"] and res["passed"] == 1 and res["fail"] is None)
    del os.environ["GVS_TEST_SECRET"]

    print()
    if failures:
        print(f"{failures} VERIFIER SANDBOX TEST(S) FAILED")
        sys.exit(1)
    print("ALL VERIFIER SANDBOX TESTS PASSED")


if __name__ == "__main__":
    main()
