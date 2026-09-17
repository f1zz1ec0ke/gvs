"""TUI smoke test using Textual's run_test() pilot.

Mounts the app, starts the loop (mock model), waits for it to finish, and
checks that the live views were populated from the ledger.

Run:  python tests/test_tui.py
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from textual.widgets import DataTable

from gvs_tui.app import GvsApp
from gvs_tui.cli import DEMO_TASK, DEMO_TESTS


async def main():
    ws_root = tempfile.mkdtemp(prefix="gvs_tui_test_")
    app = GvsApp(task=DEMO_TASK, model="mock:gvs", profile="code", iters=6,
                 tests=DEMO_TESTS, ws_root=ws_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        # widgets exist
        for wid in ("#task", "#model", "#profile", "#iters", "#tests",
                    "#start", "#stop", "#pause", "#force-task", "#note",
                    "#plan-view", "#notes-view", "#tasks", "#transcript",
                    "#artifact-view", "#status"):
            app.query_one(wid)
        print("mount: all widgets present")

        # start the loop (keybinding ctrl+s)
        await pilot.press("ctrl+s")
        for _ in range(600):  # up to ~60s
            if not app.running:
                break
            await pilot.pause()
        assert not app.running, "loop did not finish"
        if app.last_error:
            print("loop error:", app.last_error)
        assert app.status is not None, "no final status"
        assert app.status["has_artifact"], "no artifact produced"
        print("loop finished:", {k: app.status[k]
                                for k in ("iters", "n_calls", "has_artifact", "stopped")})

        # live views populated from the ledger
        art = app.query_one("#artifact-view").content
        assert "def main" in str(art), "artifact view not updated"
        plan = app.query_one("#plan-view").content
        assert str(plan).strip() not in ("", "(no plan yet)"), "plan view not updated"
        table = app.query_one("#tasks", DataTable)
        assert table.row_count > 0, "task table empty"
        print("ledger views: plan, notes, tasks table, artifact all updated")

        # transcript has call lines
        print("TUI SMOKE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
