"""The ledger-based orchestration loop.

Generalized from GVS5H's ``multiagent.py``. One model is invoked in fresh
contexts as several roles; they coordinate only through the filesystem ledger.

    primary_plan    manager writes plan.md + an initial task list
    ideation        first worker proposes distinct approaches (notes.md)
    primary_manage  manager curates the task list, picks the ONE next task,
                    or declares done
    worker          a fresh worker does that one task, rewrites notes.md,
                    updates the artifact, proposes next steps
    (verifier)      optional ground-truth check of the artifact, fed back
    finalize        one last worker attempt if the loop ends without done

Every model call is appended to transcript.jsonl and also emitted as an event
so the TUI can render live progress. A :class:`Control` lets the UI stop,
pause, or force the next task.
"""

import os
import re
import time
import queue
import threading

from . import ledger
from .chat import chat as chat_fn


class Control:
    """UI steering handle: stop, pause/resume, force the next task."""

    def __init__(self):
        self._stop = threading.Event()
        self._run = threading.Event()   # set == running
        self._run.set()                 # start in the running state
        self._force = queue.Queue()

    def stop(self):
        self._stop.set()
        self._run.set()  # unblock any pause wait so the loop can exit

    def should_stop(self):
        return self._stop.is_set()

    def is_paused(self):
        return not self._run.is_set()

    def toggle_pause(self):
        if self._run.is_set():
            self._run.clear()
            return True    # now paused
        self._run.set()
        return False       # now running

    def wait_if_paused(self):
        while not self._run.is_set():
            if self._stop.is_set():
                return
            time.sleep(0.1)

    def force_next(self, text):
        self._force.put(text.strip())

    def pop_force(self):
        try:
            return self._force.get_nowait()
        except queue.Empty:
            return None


# --- prompt builders (parameterized by profile) -----------------------------

def _plan_prompt(profile):
    return (
        "You are the PRIMARY orchestrator (manager) of a small team of workers, all "
        f"expert at {profile.domain}. "
        "Given a task, produce a short overarching plan to solve it, then a task list "
        "the workers can pick up. Respond with EXACTLY these sections:\n"
        "### PLAN\n<3-6 sentence strategy>\n"
        "### TASKS\n<3-6 bullet tasks, each a concrete unit of work>"
    )


def _ideation_prompt(profile):
    return (
        "You are the FIRST WORKER. Do NOT produce the final artifact and do NOT do the "
        f"main work. Just think about it: identify the core difficulty of this {profile.domain} "
        "task, then list SEVERAL DISTINCT candidate approaches (genuinely different routes, "
        "not variations of one idea), and note pitfalls for each. Describe each approach in "
        "prose only; a later worker will execute. Respond with EXACTLY:\n"
        "### NOTES\n<your analysis>\n"
        "### NEXT\n<bullet list of distinct approaches to try next>"
    )


def _manage_prompt(profile):
    label = "SOLUTION" if profile.kind == "code" else "ANSWER"
    return (
        "You are the PRIMARY orchestrator and manager. You OWN the task list and decide "
        "when the task is done. Review the current progress and the latest worker's "
        "result, then:\n"
        "- The LATEST WORKER RESULT may include a SAMPLE TESTS verdict from actually "
        "checking the artifact. Treat it as ground truth: only set STATUS 'done' if the "
        "artifact PASSED; if it FAILED, you MUST set STATUS 'continue' and choose a task "
        "that fixes the failing case or switches to a different approach.\n"
        "- If the current " + label.lower() + " is complete and correct, set STATUS to 'done'.\n"
        "- Otherwise CURATE the task list: merge duplicates, drop finished or irrelevant "
        "items, mark completed ones [done], and fold in ONLY genuinely new sub-tasks from "
        "the proposals. Then choose the single most valuable next task.\n"
        "- IMPORTANT: if the current artifact keeps failing, or the last worker made no "
        "real progress, do NOT keep refining the same idea. Switch to a DIFFERENT approach "
        "from the notes, or ask for a new one. You have many rounds -- use them to try "
        "distinct approaches, not to polish a stuck one.\n"
        "Respond with EXACTLY these sections:\n"
        "### STATUS\n<done|continue>\n"
        "### NEXT\n<exact text of the ONE task to do next; omit if done>\n"
        "### TASKS\n<curated list, one per line, each '- [done] ...' or '- [todo] ...'>\n"
        "### UPDATE\n<one short sentence for the user describing what you just decided and why>"
    )


def _worker_prompt(profile, goal):
    notes_fmt = (
        "### NOTES\n<the COMPLETE notes file, rewritten. You are shown the current NOTES "
        "above: fold your findings into them, keep what still matters, and DELETE anything "
        "superseded, disproven, or now obvious. This REPLACES the file, so whatever you omit "
        "is gone. Organise it as '- **Topic:** ...' bullets, under ~800 words. Do NOT use "
        "markdown headings (#, ##), bold-only lines, or ALLCAPS: lines anywhere inside this "
        "section -- the reply is split on those, so they would truncate your notes.>\n"
    )
    if profile.kind == "code":
        fence = profile.code_fence or "python"
        out_fmt = (
            f"### CODE\n```{fence}\n<the FULL updated self-contained program>\n```\n"
            + notes_fmt +
            "### NEXT\n<bullet list of remaining steps, or 'none'>\n"
            "### STATUS\n<solved|continue>"
        )
    else:
        out_fmt = (
            "### ANSWER\n<the COMPLETE deliverable for the task, written out in full -- the "
            "actual content, not a summary of it and not a description of files. This section "
            "IS the artifact: whatever is not written here does not exist.>\n"
            + notes_fmt +
            "### NEXT\n<bullet list of remaining steps, or 'none'>\n"
            "### STATUS\n<solved|continue>"
        )
    return (
        f"You are a WORKER subagent, {profile.solver_system} "
        + (profile.worker_extra + " " if profile.worker_extra else "")
        + "You have NO filesystem, tools, or file access: you cannot create, write, read, "
        "dispatch to, or reference files at any path (no /shared/..., no '.md' files, no "
        "'I wrote X.md', no 'dispatched a worker'). Do not invent file paths or claim to have "
        "written or created files -- your only output is the text below, and the deliverable "
        "goes in the ANSWER (or CODE) section. Build on the current work and notes where "
        "useful -- but if your task is to try a different approach, produce a FRESH "
        f"deliverable for that approach instead of patching the stuck one. {goal}\n"
        f"Respond with EXACTLY these sections:\n{out_fmt}"
    )


# --- main entry point --------------------------------------------------------

def run_loop(task_text, profile, model,
             ws_root=None, max_iters=None, max_tasks=None,
             verifier=None, tests=None,
             control=None, events=None, log=None, fresh=True):
    """Run the full orchestration loop for one task. Returns a status dict.

    ``fresh`` starts a clean ledger. When False the existing ledger is kept and
    the loop continues from its current state (resume).
    """
    control = control or Control()
    events = events or (lambda *a: None)
    log = log or (lambda *a: None)
    ws_root = ws_root or os.environ.get(
        "GVS_WS_ROOT", os.path.abspath(os.path.join("runs", "ws")))
    max_iters = max_iters or int(os.environ.get("GVS_MAX_ITERS", "10"))
    max_tasks = max_tasks or int(os.environ.get("GVS_MAX_TASKS", "12"))

    def say(msg):
        log(msg)
        events("log", {"line": msg})

    ws = os.path.join(ws_root, ledger.slug(task_text))
    if fresh:
        ledger.init_workspace(ws, profile.artifact_file, task_text)
    else:
        os.makedirs(ws, exist_ok=True)
        if not ledger.read(ws, "task.md").strip():
            ledger.write(ws, "task.md", task_text)
    ledger.record(ws, {"_meta": True, "t": ledger.now(), "model": model,
                       "profile": profile.name, "kind": profile.kind,
                       "max_iters": max_iters, "task": task_text,
                       "continued": not fresh})
    events("init", {"ws": ws, "model": model, "profile": profile.name,
                    "artifact_file": profile.artifact_file, "fresh": fresh})

    # meta of the most recent model call, shared with the role functions so a
    # length-truncated worker can be summarized (as in GVS5H).
    last_meta: dict = {}

    def chat(role, messages, temperature):
        meta = {}
        control.wait_if_paused()
        if control.should_stop():
            raise _Stopped()
        resp = chat_fn(model, messages, temperature=temperature, meta=meta)
        last_meta.clear()
        last_meta.update(meta)
        ledger.record(ws, {"t": ledger.now(), "role": role, "request": messages,
                           "response": resp, "reasoning": meta.get("reasoning"),
                           "finish_reason": meta.get("finish_reason"),
                           "completion_tokens": meta.get("completion_tokens"),
                           "prompt_tokens": meta.get("prompt_tokens"),
                           "provider": meta.get("provider"),
                           "attempts": meta.get("attempts"),
                           "infra_exhausted": meta.get("infra_exhausted")})
        events("call", {"role": role,
                        "request": messages,
                        "response": resp,
                        "prompt_tokens": meta.get("prompt_tokens"),
                        "completion_tokens": meta.get("completion_tokens"),
                        "finish_reason": meta.get("finish_reason"),
                        "provider": meta.get("provider"),
                        "attempts": meta.get("attempts")})
        return resp

    status_out = {"ws": ws, "model": model, "profile": profile.name}

    stopped = False
    iters = 0

    try:
        # --- plan + ideate + first manage ---
        tasks, plan_text = _primary_plan(task_text, profile, ws, chat, say, max_tasks)
        events("plan", {"n_tasks": len(tasks), "plan": plan_text})
        proposals = _ideation_worker(task_text, profile, ws, chat, say)
        events("ideation", {"n_proposals": len(proposals)})
        status, next_desc, tasks, update = _primary_manage(
            task_text, profile, ws, tasks, proposals, "ideation complete", chat, say,
            max_tasks)
        ledger.save_tasks(ws, tasks)
        events("manage", {"status": status, "next": next_desc, "update": update,
                          "tasks": tasks, "iter": 0})

        iters, prev_desc = 0, None
        while status == "continue" and next_desc and iters < max_iters:
            if control.should_stop():
                stopped = True
                break
            forced = control.pop_force()
            if forced:
                next_desc = forced
                say(f"    [user] forced next task: {forced[:60]}")
            elif prev_desc is not None and next_desc.strip().lower() == prev_desc.strip().lower():
                say(f"    [primary] reissued the same task; no progress, stopping after {iters} iters")
                break
            prev_desc = next_desc
            iters += 1
            task = {"id": iters, "desc": next_desc, "status": "in_progress", "result": ""}
            wstatus, nexts, summary, wrote = _worker(task_text, profile, ws, task, chat, say, last_meta)
            events("worker", {"task_id": iters, "task": next_desc, "status": wstatus,
                              "wrote": wrote, "summary": summary})
            res = None
            if verifier is not None and wrote:
                res = verifier(ws, profile, tests)
                if res and res.get("ran"):
                    from .verifier import sample_feedback
                    summary = sample_feedback(res) + summary
                    say(f"    [verify] {res['passed']}/{res['total']} tests passed")
                    events("verify", res)
            status, next_desc, tasks, update = _primary_manage(
                task_text, profile, ws, tasks, nexts, summary, chat, say, max_tasks)
            events("manage", {"status": status, "next": next_desc, "update": update,
                              "tasks": tasks, "iter": iters})
            if res and res.get("ran") and res["passed"] < res["total"] and status == "done":
                status = "continue"
                if not next_desc:
                    next_desc = ("The artifact fails the sample tests; fix it or try a "
                                 "different approach.")
                say("    [primary-manage] overriding 'done' -- tests still failing")
            ledger.save_tasks(ws, tasks)

        if not stopped:
            if status == "done" and ledger.has_artifact(ws, profile):
                say("    [finalize] skipped (manager marked done)")
            else:
                _worker(task_text, profile, ws, {"id": 0, "desc": "finalize"}, chat, say,
                        last_meta, finalize=True)
                events("finalize", {})
    except _Stopped:
        stopped = True

    if stopped:
        say("    [stopped] user interrupted the loop")

    # --- status summary ---
    recs = [r for r in ledger.read_transcript(ws) if not r.get("_meta")]
    status_out.update({
        "stopped": stopped,
        "n_calls": len(recs),
        "iters": iters,
        "finish_reason": recs[-1].get("finish_reason") if recs else None,
        "truncated_calls": sum(1 for r in recs if r.get("finish_reason") == "length"),
        "infra_fail": any(r.get("infra_exhausted") for r in recs),
        "has_artifact": ledger.has_artifact(ws, profile),
        "artifact_file": profile.artifact_file,
    })
    events("done", status_out)
    return status_out


class _Stopped(Exception):
    pass


# --- roles (each a fresh context) --------------------------------------------

def _chat_block(ws, n=10):
    """Recent user<->manager chat, as a prompt block (empty if none)."""
    ctx = ledger.recent_chat(ws, n)
    if not ctx:
        return ""
    return ("\n\nRECENT CHAT WITH THE USER (incorporate their direction):\n" + ctx)


def _primary_plan(task_text, profile, ws, chat, say, max_tasks):
    reply = chat("primary_plan", [
        {"role": "system", "content": _plan_prompt(profile)},
        {"role": "user", "content": task_text + _chat_block(ws)},
    ], profile.temps.get("plan", 0.3))
    sec = ledger.sections(reply)
    plan_text = (sec.get("PLAN") or reply.strip())[:ledger.MAX_PLAN_CHARS]
    ledger.write(ws, "plan.md", plan_text)
    tasks = ledger.parse_tasks(sec.get("TASKS", ""), max_tasks)
    say(f"    [primary] plan + {len(tasks)} initial tasks")
    return tasks, plan_text


def _ideation_worker(task_text, profile, ws, chat, say):
    reply = chat("ideation", [
        {"role": "system", "content": _ideation_prompt(profile)},
        {"role": "user", "content": f"TASK:\n{task_text}\n\nPLAN:\n{ledger.read(ws, 'plan.md')}"},
    ], profile.temps.get("ideation", 0.4))
    sec = ledger.sections(reply)
    ledger.append(ws, "notes.md",
                  f"\n## ideation\n{ledger.strip_code(sec.get('NOTES') or reply.strip())[:ledger.MAX_PLAN_CHARS * 2]}\n")
    proposals = [ledger.strip_code(p) for p in ledger.bullets(sec.get("NEXT", ""))]
    say(f"    [worker:ideate] proposed {len(proposals)} approaches")
    return proposals


def _primary_manage(task_text, profile, ws, tasks, proposals, last_summary, chat, say,
                    max_tasks):
    cur = ledger.read(ws, profile.artifact_file).strip()
    task_lines = "\n".join(
        f"- [{'done' if t['status'] == 'done' else 'todo'}] {t['desc']}" for t in tasks
    ) or "(none)"
    prop_lines = "\n".join(f"- {p}" for p in proposals) or "(none)"
    label = "SOLUTION" if profile.kind == "code" else "ANSWER"
    reply = chat("primary_manage", [
        {"role": "system", "content": _manage_prompt(profile)},
        {"role": "user", "content": (
            f"TASK:\n{task_text}\n\nCURRENT {label}:\n{cur or '(none yet)'}\n\n"
            f"NOTES:\n{ledger.read(ws, 'notes.md')}\n\n"
            f"CURRENT TASK LIST:\n{task_lines}\n\n"
            f"LATEST WORKER RESULT: {last_summary}\n\n"
            f"PROPOSED NEW STEPS:\n{prop_lines}" + _chat_block(ws)
        )},
    ], profile.temps.get("manage", 0.2))
    sec = ledger.sections(reply)
    update = sec.get("UPDATE", "").strip()
    curated = ledger.parse_tasks(sec.get("TASKS", ""), max_tasks) or tasks
    status = "done" if sec.get("STATUS", "").strip().lower().startswith("done") else "continue"
    if status == "done" and not cur:  # invariant: can't be done with no artifact yet
        status = "continue"
    nxt = (ledger.bullets(sec.get("NEXT", ""))[:1] or [sec.get("NEXT", "").strip()])[0]
    nxt = re.sub(r"^[-*]\s*", "", nxt).strip()
    if status == "continue" and not nxt:
        todo = [t for t in curated if t["status"] != "done"]
        nxt = todo[0]["desc"] if todo else ""
    if not cur and not nxt:
        status, nxt = "continue", "Produce the working artifact for the most promising approach in the notes."
    say(f"    [primary-manage] {status}" + (f", next: {nxt[:60]}" if status == "continue" else ""))
    return status, nxt, curated, update


def _summarize_cutoff(ws, profile, reply, task_desc, chat):
    txt = ledger.strip_think(reply) or reply
    snippet = txt if len(txt) <= 9000 else txt[:3500] + "\n...[middle omitted]...\n" + txt[-5500:]
    sysmsg = (
        "A worker's attempt was CUT OFF when it hit the token limit. Summarize its "
        "partial attempt in 3-5 sentences: which approach it was pursuing, what it "
        "established or ruled out, how far it got, and what remained unfinished. Be "
        "concrete so another worker can resume or judge it. Do NOT try to finish the "
        "artifact yourself."
    )
    s = chat("cutoff_summary", [
        {"role": "system", "content": sysmsg},
        {"role": "user", "content": f"TASK: {task_desc}\n\nCUT-OFF ATTEMPT:\n{snippet}"},
    ], profile.temps.get("cutoff", 0.2))
    return ledger.strip_think(s).strip() or "(the cut-off attempt could not be summarized)"


def _worker(task_text, profile, ws, task, chat, say, last_meta, finalize=False):
    cur = ledger.read(ws, profile.artifact_file)
    goal = ("Produce the DEFINITIVE final artifact now, using all notes and current work."
            if finalize else f"Complete this task: {task['desc']}")
    sys = _worker_prompt(profile, goal)
    reply = chat("finalize" if finalize else f"worker:{task['id']}", [
        {"role": "system", "content": sys},
        {"role": "user", "content": (
            f"TASK:\n{task_text}\n\nPLAN:\n{ledger.read(ws, 'plan.md')}\n\n"
            f"NOTES:\n{ledger.read(ws, 'notes.md')}\n\n"
            f"CURRENT WORK:\n{cur or '(none yet)'}\n\n"
            f"YOUR TASK: {goal}"
        )},
    ], profile.temps.get("worker", 0.2))
    sec = ledger.sections(reply)
    wrote = False
    if profile.kind == "code":
        fence = profile.code_fence or "python"
        code = (ledger.extract_fenced(sec.get("CODE", ""), fence)
                or ledger.extract_fenced(reply, fence))
        if code:
            ledger.write(ws, profile.artifact_file, code)
            wrote = True
    else:
        ans = sec.get("ANSWER", "").strip()
        if not ans and profile.answer_marker and re.search(profile.answer_marker, reply, re.I) \
                and len(reply) < ledger.MAX_ANSWER_CHARS:
            ans = ledger.strip_think(reply).strip()
        if ans and (not profile.answer_marker or re.search(profile.answer_marker, ans, re.I)
                    or not ledger.read(ws, profile.artifact_file).strip()):
            ledger.write(ws, profile.artifact_file, ans)
            wrote = True
    if sec.get("NOTES") and last_meta.get("finish_reason") != "length":
        ledger.write(ws, "notes.md", sec["NOTES"].strip()[:ledger.MAX_PLAN_CHARS * 2] + "\n")
    nexts = [b for b in ledger.bullets(sec.get("NEXT", "")) if b.lower() != "none"]
    status = sec.get("STATUS", "continue").strip().lower()
    if last_meta.get("finish_reason") == "length" and not finalize:
        digest = _summarize_cutoff(ws, profile, reply, task["desc"], chat)
        summary = (f"worker EXCEEDED THE TOKEN LIMIT and was cut off before finishing task: "
                   f"{task['desc'][:80]}. No complete artifact was produced -- the approach was "
                   "too long to finish in one call, so prefer a simpler or different approach "
                   f"next. Summary of its partial work: {digest[:500]}")
        say(f"    [worker] task {task['id']} -> CUT OFF at token limit; summarized, told manager")
        return "continue", nexts, summary, wrote
    summary = f"worker reported '{status}' on: {task['desc'][:80]}. " + (sec.get("NOTES", "")[:200])
    say(f"    [worker] task {task['id']} -> {status}, +{len(nexts)} next steps")
    return status, nexts, summary, wrote
