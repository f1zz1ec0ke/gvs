"""gvs-tui: a general-purpose TUI harness for ledger-based zero-shot
self-orchestration.

The paradigm (from the GVS5H / "Gorilla vs 5 Humans" paper): one model is
invoked in *fresh contexts* as different roles -- a manager that owns a plan
and a live task list, an ideation worker, and worker subagents that each do
one task and rewrite shared notes. All roles coordinate through a shared
filesystem *ledger* (task.md, plan.md, notes.md, tasks.json, the current
artifact, and a transcript). This package generalizes that loop beyond
benchmarking: pluggable domain profiles, optional verifier hooks, and a live
terminal UI.
"""

__version__ = "0.1.0"
