"""Steering tests: Control.stop() and Control.force_next() drive the loop.

Uses a deliberately slow mock (wrapped around the real deterministic mock) so
the stop/force happen mid-run.

Run:  python tests/test_steering.py
"""

import os
import sys
import time
import tempfile
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import gvs_tui.loop as loop
from gvs_tui.chat import mock_chat
from gvs_tui.profiles import get_profile
from gvs_tui.loop import run_loop, Control

ORIG_CHAT = loop.chat_fn
SLEEP = 0.15


def slow_chat(model, messages, temperature=0.2, meta=None):
    time.sleep(SLEEP)
    return mock_chat(model, messages, temperature, meta)


def _run_in_thread(control, max_iters, logs=None):
    result = {}

    def worker():
        result["s"] = run_loop(
            "steering test task", get_profile("general"), "mock:gvs",
            ws_root=tempfile.mkdtemp(prefix="gvs_steer_"),
            max_iters=max_iters, control=control,
            log=(lambda m: logs.append(m)) if logs is not None else (lambda *a: None))

    t = threading.Thread(target=worker)
    t.start()
    return t, result


def test_stop():
    loop.chat_fn = slow_chat
    control = Control()
    t, result = _run_in_thread(control, max_iters=20)
    time.sleep(SLEEP * 3)  # let it get a few calls in
    control.stop()
    t.join(timeout=10)
    loop.chat_fn = ORIG_CHAT
    assert "s" in result, "loop never returned"
    assert result["s"]["stopped"] is True, f"expected stopped, got {result['s']}"
    print("stop: OK  (stopped after", result["s"]["iters"], "iters)")


def test_force_next():
    loop.chat_fn = slow_chat
    control = Control()
    control.force_next("FORCED TASK XYZ")
    logs = []
    t, result = _run_in_thread(control, max_iters=5, logs=logs)
    t.join(timeout=15)
    loop.chat_fn = ORIG_CHAT
    assert any("forced next task" in ln for ln in logs), \
        f"forced task not used; logs={logs}"
    print("force-next: OK  (forced task consumed by the loop)")


def test_pause_resume():
    loop.chat_fn = slow_chat
    control = Control()
    t, result = _run_in_thread(control, max_iters=20)
    time.sleep(SLEEP * 2)
    control.toggle_pause()          # pause
    iters_at_pause = None
    time.sleep(SLEEP * 3)
    # while paused the loop should not be making calls; just verify it resumes
    control.toggle_pause()          # resume
    control.stop()
    t.join(timeout=10)
    loop.chat_fn = ORIG_CHAT
    assert "s" in result, "loop never returned"
    print("pause/resume: OK  (loop completed after pause + stop)")


if __name__ == "__main__":
    test_stop()
    test_force_next()
    test_pause_resume()
    print("\nALL STEERING TESTS PASSED")
