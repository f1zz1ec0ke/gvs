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
from . import tools
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
    return (
        "You are the PRIMARY orchestrator and manager. You OWN the task list and decide "
        "when the task is done. The deliverable is the set of files under 'out/' (workers "
        "write it there). Review the current progress and the latest worker's result, then:\n"
        "- The LATEST WORKER RESULT may include a SAMPLE TESTS verdict from actually "
        "checking the deliverable. Treat it as ground truth: only set STATUS 'done' if the "
        "deliverable PASSED; if it FAILED, you MUST set STATUS 'continue' and choose a task "
        "that fixes the failing case or switches to a different approach.\n"
        "- If the current deliverable is complete and correct, set STATUS to 'done'.\n"
        "- Otherwise CURATE the task list: merge duplicates, drop finished or irrelevant "
        "items, mark completed ones [done], and fold in ONLY genuinely new sub-tasks from "
        "the proposals. Then choose the single most valuable next task.\n"
        "- IMPORTANT: if the current artifact keeps failing, or the last worker made no "
        "real progress, do NOT keep refining the same idea. Switch to a DIFFERENT approach "
        "from the notes, or ask for a new one. You have many rounds -- use them to try "
        "distinct approaches, not to polish a stuck one.\n"
        "- If the task needs EXTERNAL or CURRENT information (facts, documentation, recent "
        "events, specs, versions, prices, news) that a worker cannot reliably know, prefix "
        "your NEXT task with '[research]' and state exactly what to find out, e.g. "
        "'[research] Find the current stable version of X and what changed in its API'. A "
        "dedicated researcher with web search will gather it into the shared workspace.\n"
        + tools.READ_ONLY_PROTOCOL
        + "Respond with EXACTLY these sections:\n"
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
    log_fmt = (
        "### LOG\n<one or two short bullets (under ~30 words) of what you FOUND, DECIDED, or "
        "DROPPED this turn -- the delta only, not a restatement of the notes. The loop appends "
        "this verbatim to the append-only 'notes.log.md' history; you do not edit that file.>\n"
    )
    deliverable = ("Write EVERY deliverable file under the 'out/' directory using write_file "
                   "(create the directory; everything under 'out/' is the deliverable). "
                   "A task may need one file or many (e.g. a website is index.html + css + js).")
    fallback = ("If you cannot use the file tools, put the full deliverable in an "
                "### ANSWER section instead.")
    final_fmt = (
        "### STATUS\n<solved|continue>\n"
        "### NEXT\n<bullet list of remaining steps, or 'none'>\n"
        + log_fmt
        + notes_fmt
    )
    return (
        f"You are a WORKER subagent, {profile.solver_system} "
        + (profile.worker_extra + " " if profile.worker_extra else "")
        + "You have a shared workspace and FILE TOOLS. Use them to ACTUALLY create and edit "
        "files -- do not merely describe files you 'wrote'. " + deliverable
        + " You may also create other files (drafts) as needed. You maintain TWO notes files: "
        "'notes.md' is the curated shared state -- REWRITE it each turn (under ~800 words); "
        "'notes.log.md' is an append-only history you do NOT edit (the loop appends your "
        "### LOG entry to it). If a fact seems missing from 'notes.md', read 'notes.log.md' "
        "to recover it. If a 'research.md' file exists (from a researcher), read it with "
        "read_file and use its findings.\n"
        + tools.TOOL_PROTOCOL
        + "Build on the current work and notes where useful -- but if your task is to try a "
        "different approach, produce a FRESH deliverable for that approach instead of "
        f"patching the stuck one. {goal}\n"
        + fallback + "\n"
        "When you are done, respond with EXACTLY these sections and NO tool block:\n"
        + final_fmt
    )


def _researcher_prompt(profile, goal):
    notes_fmt = (
        "### NOTES\n<the COMPLETE notes file, rewritten. Fold the key research findings "
        "into it (with source URLs) so the workers see the essence. Keep it under ~800 words.>\n"
    )
    log_fmt = (
        "### LOG\n<one or two short bullets (under ~30 words) of what you FOUND this turn -- "
        "the key facts/sources, not a restatement of the notes. Appended to the append-only "
        "'notes.log.md' history by the loop; you do not edit that file.>\n"
    )
    return (
        f"You are a RESEARCHER subagent, {profile.solver_system} "
        + (profile.worker_extra + " " if profile.worker_extra else "")
        + "You have FILE TOOLS and WEB TOOLS (search + fetch). Your job is to gather reliable, "
        "current information for the task and put it in the shared workspace so other workers "
        "can use it. Use search to find sources, fetch the most relevant ones to read the "
        "actual content, and write your findings to 'research.md' (create it with write_file; "
        "you may also create research/<topic>.md for detailed notes). Cite sources by URL and "
        "note anything you could NOT verify. If the search tool reports that no provider is "
        "configured, do NOT retry it -- use your own knowledge and flag that you could not "
        "verify externally. When done, rewrite 'notes.md' with a concise "
        f"summary of the key findings. {goal}\n"
        + tools.TOOL_PROTOCOL
        + tools.WEB_PROTOCOL
        + "When you are done, respond with EXACTLY these sections and NO tool block:\n"
        "### STATUS\n<solved|continue>\n"
        "### NEXT\n<bullet list of remaining steps, or 'none'>\n"
        + log_fmt
        + notes_fmt
    )


# --- main entry point --------------------------------------------------------

def run_loop(task_text, profile, model,
             ws_root=None, max_iters=None, max_tasks=None,
             verifier=None, tests=None,
             control=None, events=None, log=None, fresh=True,
             search=None, ws=None):
    """Run the full orchestration loop for one task. Returns a status dict.

    ``fresh`` starts a clean ledger. When False the existing ledger is kept and
    the loop continues from its current state (resume). If ``ws`` is given, the
    run is pinned to that workspace (instead of deriving one from the task
    slug), so a run and its triggering chat always share the same workspace.
    """
    control = control or Control()
    events = events or (lambda *a: None)
    log = log or (lambda *a: None)
    ws_root = ws_root or os.environ.get(
        "GVS_WS_ROOT", os.path.abspath(os.path.join("runs", "ws")))
    max_iters = max_iters or int(os.environ.get("GVS_MAX_ITERS", "10"))
    max_tasks = max_tasks or int(os.environ.get("GVS_MAX_TASKS", "12"))

    def say(msg):
        try:
            log(msg)
        except Exception:  # noqa: BLE001 - a logger that can't encode msg must not kill the run
            try:
                log(msg.encode("ascii", "replace").decode("ascii"))
            except Exception:  # noqa: BLE001
                pass
        events("log", {"line": msg})

    if ws is None:
        ws = os.path.join(ws_root, ledger.slug(task_text))
    if fresh:
        ledger.init_workspace(ws, task_text)
    else:
        os.makedirs(ws, exist_ok=True)
        os.makedirs(os.path.join(ws, ledger.OUT_DIR), exist_ok=True)
        if not ledger.read(ws, "task.md").strip():
            ledger.write(ws, "task.md", task_text)
    ledger.record(ws, {"_meta": True, "t": ledger.now(), "model": model,
                       "profile": profile.name, "kind": profile.kind,
                       "max_iters": max_iters, "task": task_text,
                       "continued": not fresh})
    events("init", {"ws": ws, "model": model, "profile": profile.name,
                    "artifact_dir": ledger.OUT_DIR, "fresh": fresh})

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

    def _short_args(args):
        if not args:
            return ""
        parts = []
        for k, v in args.items():
            if k == "content" and isinstance(v, str):
                v = f"<{len(v)} chars>"
            parts.append(f"{k}={v}")
        return ", ".join(parts)

    def chat_tools(role, messages, temperature, read_only=False, web=False,
                   search_provider=None, max_calls=None):
        """Run a tool loop: call the model, execute any tool call (confined to ``ws``),
        feed the result back, and repeat until the model gives a final (non-tool)
        answer or the call cap is hit. Returns ``(final_text, tool_log)``.

        ``web`` enables the search/fetch tools (researcher only); ``search_provider``
        is the configured SearchProvider. Each model call is still recorded in the
        transcript and emitted as a ``call`` event, so every tool step is auditable
        and visible live in the GUI.
        """
        max_calls = max_calls or tools.MAX_TOOL_CALLS
        msgs = list(messages)
        tool_log = []
        resp = ""
        for _step in range(max_calls + 1):
            resp = chat(role, msgs, temperature)
            call = tools.parse_tool_block(resp)
            if call is None:
                break  # final answer (no tool block)
            result = tools.run_tool(call["name"], call["args"], ws, read_only=read_only,
                                    web=web, search_provider=search_provider)
            tool_log.append({"name": call["name"], "args": call["args"], "result": result})
            first = (result.splitlines() or ["(empty)"])[0][:90]
            say(f"    [tool:{role}] {call['name']}({_short_args(call['args'])}) -> {first}")
            msgs.append({"role": "assistant", "content": resp})
            msgs.append({"role": "user", "content":
                        f"TOOL RESULT for {call['name']}:\n{result}\n\n"
                        "Continue. If you are finished, give your final sections now "
                        "(with NO tool block)."})
        return resp, tool_log

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
            is_research = next_desc.strip().lower().startswith("[research]")
            task_desc = re.sub(r"^\[research\]\s*", "", next_desc, flags=re.I).strip() or next_desc
            task = {"id": iters, "desc": task_desc, "status": "in_progress", "result": ""}
            if is_research:
                wstatus, nexts, summary, _wrote = _researcher(
                    task_text, profile, ws, task, chat, say, last_meta, chat_tools, search)
                events("researcher", {"task_id": iters, "task": next_desc, "status": wstatus,
                                      "summary": summary})
            else:
                wstatus, nexts, summary, wrote = _worker(
                    task_text, profile, ws, task, chat, say, last_meta, chat_tools=chat_tools)
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
                task_text, profile, ws, tasks, nexts, summary, chat, say, max_tasks,
                chat_tools=chat_tools)
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
            if status == "done" and ledger.has_artifact(ws):
                say("    [finalize] skipped (manager marked done)")
            else:
                _worker(task_text, profile, ws, {"id": 0, "desc": "finalize"}, chat, say,
                        last_meta, finalize=True, chat_tools=chat_tools)
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
        "has_artifact": ledger.has_artifact(ws),
        "artifact_dir": ledger.OUT_DIR,
        "n_artifact_files": len(ledger.out_files(ws)),
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
                    max_tasks, chat_tools=None):
    cur = ledger.out_brief(ws)
    task_lines = "\n".join(
        f"- [{'done' if t['status'] == 'done' else 'todo'}] {t['desc']}" for t in tasks
    ) or "(none)"
    prop_lines = "\n".join(f"- {p}" for p in proposals) or "(none)"
    user_msg = (
        f"TASK:\n{task_text}\n\nCURRENT DELIVERABLE (files under 'out/'):\n"
        f"{cur or '(none yet)'}\n\n"
        f"NOTES:\n{ledger.read(ws, 'notes.md')}\n\n"
        f"WORKSPACE FILES:\n{tools.file_listing(ws)}\n\n"
        f"CURRENT TASK LIST:\n{task_lines}\n\n"
        f"LATEST WORKER RESULT: {last_summary}\n\n"
        f"PROPOSED NEW STEPS:\n{prop_lines}" + _chat_block(ws)
    )
    messages = [
        {"role": "system", "content": _manage_prompt(profile)},
        {"role": "user", "content": user_msg},
    ]
    if chat_tools is not None:
        reply, _tool_log = chat_tools("primary_manage", messages,
                                      profile.temps.get("manage", 0.2), read_only=True)
    else:
        reply = chat("primary_manage", messages, profile.temps.get("manage", 0.2))
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


def _worker(task_text, profile, ws, task, chat, say, last_meta, finalize=False, chat_tools=None):
    cur = ledger.out_snapshot(ws)
    before_notes = ledger.read(ws, "notes.md")
    goal = ("Produce the DEFINITIVE final deliverable now, using all notes and current work."
            if finalize else f"Complete this task: {task['desc']}")
    sys = _worker_prompt(profile, goal)
    role = "finalize" if finalize else f"worker:{task['id']}"
    user_msg = (
        f"TASK:\n{task_text}\n\nPLAN:\n{ledger.read(ws, 'plan.md')}\n\n"
        f"NOTES:\n{ledger.read(ws, 'notes.md')}\n\n"
        f"CURRENT DELIVERABLE (files under 'out/'):\n{ledger.out_brief(ws) or '(none yet)'}\n\n"
        f"WORKSPACE FILES:\n{tools.file_listing(ws)}\n\n"
        f"YOUR TASK: {goal}\n"
        f"Write your deliverable file(s) under 'out/' using write_file."
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_msg},
    ]
    if chat_tools is not None:
        reply, tool_log = chat_tools(role, messages, profile.temps.get("worker", 0.2))
    else:
        reply, tool_log = chat(role, messages, profile.temps.get("worker", 0.2)), []
    sec = ledger.sections(reply)

    # The deliverable is the 'out/' directory: did it change this turn?
    after = ledger.out_snapshot(ws)
    wrote = after != cur

    # Fallback for models that don't use the file tools: if 'out/' is still empty
    # but the reply carries a deliverable, save it to out/result.md.
    if not wrote:
        ans = sec.get("ANSWER", "").strip() or ledger.strip_think(reply).strip()
        if ans and len(ans) < ledger.MAX_ANSWER_CHARS:
            ledger.write(ws, f"{ledger.OUT_DIR}/result.md", ans)
            after = ledger.out_snapshot(ws)
            wrote = after != cur

    # Notes: if the worker didn't update notes.md itself, fall back to ### NOTES.
    if (ledger.read(ws, "notes.md").strip() == before_notes.strip()
            and sec.get("NOTES") and last_meta.get("finish_reason") != "length"):
        ledger.write(ws, "notes.md", sec["NOTES"].strip()[:ledger.MAX_PLAN_CHARS * 2] + "\n")

    # Append the concise delta to the append-only history (notes.log.md).
    if last_meta.get("finish_reason") == "length":
        log_entry = "cut off at the token limit before finishing"
    else:
        log_entry = sec.get("LOG", "").strip()
        if not log_entry:
            log_entry = f"reported '{sec.get('STATUS', 'continue').strip().lower()}'"
    ledger.append_log(ws, role, log_entry)

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
    tool_note = f" [tools: {', '.join(t['name'] for t in tool_log)}]" if tool_log else ""
    summary = f"worker reported '{status}' on: {task['desc'][:80]}. " \
              + (sec.get("NOTES", "")[:200]) + tool_note
    say(f"    [worker] task {task['id']} -> {status}, +{len(nexts)} next steps{tool_note}")
    return status, nexts, summary, wrote


def _researcher(task_text, profile, ws, task, chat, say, last_meta, chat_tools, search):
    """A dedicated researcher: gathers external information with the web tools and
    writes it to the shared workspace (research.md + notes.md) for workers to use.

    Returns ``(status, nexts, summary, wrote)``; ``wrote`` is always False because the
    researcher does not produce the profile's artifact (so the verifier is not run).
    """
    goal = f"Research this task and gather the relevant facts and sources: {task['desc']}"
    sys = _researcher_prompt(profile, goal)
    role = f"researcher:{task['id']}"
    before_notes = ledger.read(ws, "notes.md")
    user_msg = (
        f"TASK:\n{task_text}\n\nPLAN:\n{ledger.read(ws, 'plan.md')}\n\n"
        f"NOTES:\n{ledger.read(ws, 'notes.md')}\n\n"
        f"EXISTING RESEARCH (research.md):\n{ledger.read(ws, 'research.md') or '(none yet)'}\n\n"
        f"YOUR TASK: {goal}\n"
        "Write your findings to 'research.md' (create or extend it) and update 'notes.md' "
        "with a concise summary of the key findings."
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_msg},
    ]
    reply, tool_log = chat_tools(role, messages, profile.temps.get("worker", 0.2),
                                 web=True, search_provider=search)
    sec = ledger.sections(reply)
    # Notes: if the researcher didn't update notes.md itself, fall back to ### NOTES.
    if (ledger.read(ws, "notes.md").strip() == before_notes.strip()
            and sec.get("NOTES") and last_meta.get("finish_reason") != "length"):
        ledger.write(ws, "notes.md", sec["NOTES"].strip()[:ledger.MAX_PLAN_CHARS * 2] + "\n")
    # Append the concise delta to the append-only history (notes.log.md).
    log_entry = sec.get("LOG", "").strip() or \
        f"reported '{sec.get('STATUS', 'continue').strip().lower()}'"
    ledger.append_log(ws, role, log_entry)
    # Fallback: if research.md wasn't written via a tool, write the findings there.
    # Strip any tool block so a raw tool call is never written to the file.
    if not ledger.read(ws, "research.md").strip():
        findings = (sec.get("NOTES") or ledger.strip_think(reply) or reply).strip()
        findings = re.sub(r"```tool[\s\S]*?```", "", findings).strip()
        if findings:
            ledger.write(ws, "research.md", findings[:ledger.MAX_PLAN_CHARS * 2] + "\n")
    nexts = [b for b in ledger.bullets(sec.get("NEXT", "")) if b.lower() != "none"]
    status = sec.get("STATUS", "continue").strip().lower()
    tool_note = f" [tools: {', '.join(t['name'] for t in tool_log)}]" if tool_log else ""
    summary = f"researcher reported '{status}' on: {task['desc'][:80]}. " \
              + (sec.get("NOTES", "")[:200]) + tool_note
    say(f"    [researcher] task {task['id']} -> {status}{tool_note}")
    return status, nexts, summary, False
