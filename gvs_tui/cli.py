"""Command-line entry point.

    gvs-tui                          # TUI, mock model, demo task (no key needed)
    gvs-tui --task "..." --model openai:gpt-5 --profile code
    gvs-tui --task-file task.md --headless        # no TUI, logs to stdout
    gvs-tui --mock --headless                     # full loop, no key, no TUI
    gvs-tui --web [--port 8321]                   # web GUI (browser)
"""

import os
import sys
import argparse

from .profiles import PROFILES, get_profile
from .verifier import run_python_tests
from .loop import run_loop

DEMO_TASK = ("Read a single integer n from stdin and print n*n to stdout. "
             "Input constraints: 1 <= n <= 10^9.")
DEMO_TESTS = "3 => 9\n1 => 1"


def build_parser():
    p = argparse.ArgumentParser(
        prog="gvs-tui",
        description="General-purpose TUI harness for ledger-based zero-shot "
                    "self-orchestration (the GVS5H paradigm).")
    p.add_argument("--task", help="task text (inline)")
    p.add_argument("--task-file", help="path to a file containing the task text")
    p.add_argument("--model", default=None, help="provider:model (e.g. mock:gvs, "
                                                 "openai:gpt-5, ollama model name)")
    p.add_argument("--profile", default=None, choices=sorted(PROFILES),
                   help="domain profile (default: code with --mock, else general)")
    p.add_argument("--iters", type=int, default=None,
                   help="max manager iterations (default 10, or $GVS_MAX_ITERS)")
    p.add_argument("--tests", default=None,
                   help="sample tests for the code profile, one 'input => output' per line")
    p.add_argument("--ws-root", default=None, help="where task workspaces are created")
    p.add_argument("--mock", action="store_true",
                   help="use the deterministic mock model (no API key needed)")
    p.add_argument("--headless", action="store_true",
                   help="run the loop without the TUI; log to stdout")
    p.add_argument("--web", action="store_true",
                   help="launch the web GUI (FastAPI + WebSocket) instead of the TUI")
    p.add_argument("--host", default="127.0.0.1", help="web server host (with --web)")
    p.add_argument("--port", type=int, default=8321, help="web server port (with --web)")
    return p


def _read_task(args):
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as f:
            return f.read().strip()
    if args.task:
        return args.task
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _resolve(args):
    task = _read_task(args)
    mock = args.mock or (args.model is None and not task)
    model = args.model if args.model else ("mock:gvs" if mock else None)
    if not model:
        print("error: no model given and no --mock; pass --model provider:model or --mock",
              file=sys.stderr)
        sys.exit(2)
    profile_name = args.profile or ("code" if mock else "general")
    if not task:
        task = DEMO_TASK
        print("(no task provided -- using the built-in demo task)", file=sys.stderr)
    tests_raw = args.tests
    if not tests_raw and mock and get_profile(profile_name).kind == "code":
        tests_raw = DEMO_TESTS
    return task, model, profile_name, tests_raw


def _parse_tests(raw):
    tests = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if "=>" in line:
            inp, out = line.split("=>", 1)
            tests.append({"input": inp.strip(), "output": out.strip()})
    return tests


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.web:
        try:
            from .web.server import main as web_main
        except ImportError as e:
            print(f"error: web dependencies missing ({e}). "
                  f"Install with: pip install 'gvs-tui[web]'", file=sys.stderr)
            sys.exit(2)
        web_main(host=args.host, port=args.port)
        return

    task, model, profile_name, tests_raw = _resolve(args)
    profile = get_profile(profile_name)
    tests = _parse_tests(tests_raw)
    verifier = run_python_tests if (profile.kind == "code" and tests) else None

    if args.headless:
        status = run_loop(
            task, profile, model,
            ws_root=args.ws_root,
            max_iters=args.iters,
            verifier=verifier, tests=tests,
            log=lambda m: print(m, flush=True),
        )
        print(f"\nworkspace : {status['ws']}")
        print(f"iters     : {status['iters']}   calls: {status['n_calls']}")
        print(f"artifact  : {'yes' if status['has_artifact'] else 'NO'} "
              f"({status['artifact_file']})")
        art_path = os.path.join(status["ws"], status["artifact_file"])
        if status["has_artifact"]:
            print("\n--- artifact ---")
            with open(art_path, encoding="utf-8") as f:
                print(f.read())
        return

    from .app import GvsApp
    app = GvsApp(task=task, model=model, profile=profile_name,
                 iters=args.iters or 10, tests=tests_raw or "",
                 ws_root=args.ws_root)
    app.run()


if __name__ == "__main__":
    main()
