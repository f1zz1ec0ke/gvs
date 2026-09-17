"""Domain profiles.

A profile adapts the orchestration loop to a domain *without* changing the
loop itself: it supplies the domain name, the worker's solver system prompt,
the artifact file and its kind, and (for answer-style artifacts) the marker
that signals a final answer. This is what lets the same manager/worker loop
drive coding tasks, math problems, research questions, or arbitrary work.

Add your own by constructing a :class:`Profile` and registering it, or by
passing one straight into :func:`gvs_tui.loop.run_loop`.
"""

from dataclasses import dataclass, field


@dataclass
class Profile:
    name: str
    # "code" -> artifact is a program extracted from a fenced block;
    # "answer" -> artifact is free text (a worked solution / report / answer).
    kind: str = "answer"
    # One-line domain description used in the manager/worker prompts.
    domain: str = "the task at hand"
    # Worker (solver) system prompt.
    solver_system: str = (
        "You are a capable generalist. Produce a complete, self-contained written "
        "result for the task: state the approach, carry it through to a concrete "
        "result, and note any caveats."
    )
    # File that holds the current artifact.
    artifact_file: str = "result.md"
    # For kind == "code": the fence language used when extracting the program.
    code_fence: str | None = None
    # For kind == "answer": regex that marks a final answer (None = any non-empty text).
    answer_marker: str | None = None
    # Per-role temperatures.
    temps: dict = field(default_factory=lambda: {
        "plan": 0.3, "ideation": 0.4, "manage": 0.2, "worker": 0.2, "cutoff": 0.2,
    })
    # Optional extra instructions appended to the worker's system prompt.
    worker_extra: str = ""


# --- built-in profiles -------------------------------------------------------

CODE = Profile(
    name="code",
    kind="code",
    domain="competitive programming and software engineering",
    solver_system=(
        "You are an elite competitive programmer. Solve the given problem in Python. "
        "Think carefully about algorithmic complexity and edge cases. Output EXACTLY ONE "
        "complete, self-contained Python program inside a single ```python ...``` fenced "
        "block, and nothing else after it."
    ),
    artifact_file="solution.py",
    code_fence="python",
)

MATH = Profile(
    name="math",
    kind="answer",
    domain="olympiad-level mathematics",
    solver_system=(
        "You are an elite mathematician. Solve the problem. Reason step by step, then on "
        "the FINAL line output exactly 'ANSWER: <final answer>' with the answer in simplest "
        "form (a number or simplified expression, written as it would appear in a textbook)."
    ),
    artifact_file="answer.md",
    answer_marker=r"ANSWER:\s*\S",
)

RESEARCH = Profile(
    name="research",
    kind="answer",
    domain="technical research and analysis",
    solver_system=(
        "You are a rigorous research analyst. Investigate the question, weigh the evidence, "
        "and write a clear, well-structured report: background, analysis, and a crisp "
        "bottom line. On the FINAL line output exactly 'CONCLUSION: <one or two sentences>'."
    ),
    artifact_file="report.md",
    answer_marker=r"CONCLUSION:\s*\S",
)

GENERAL = Profile(
    name="general",
    kind="answer",
    domain="the task at hand",
    solver_system=(
        "You are a capable generalist. Complete the task end to end and write a complete, "
        "self-contained result: what you did, the concrete outcome, and any caveats or next "
        "steps. Be specific and finish with the actual deliverable, not a summary of intent."
    ),
    artifact_file="result.md",
    answer_marker=None,
)


PROFILES: dict[str, Profile] = {
    p.name: p for p in (CODE, MATH, RESEARCH, GENERAL)
}


def get_profile(name):
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(f"unknown profile '{name}'; available: {', '.join(sorted(PROFILES))}")


def register(profile):
    PROFILES[profile.name] = profile
    return profile
