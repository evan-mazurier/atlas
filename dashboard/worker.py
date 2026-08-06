"""
Atlas dispatch worker — RUNS ON WINDOWS (where claude.exe + git live).

Watches the job queue (D:\\work\\Atlas\\jobs) and, for each queued job, runs a headless
Claude Code session inside a sandbox:
  - git repo  -> an isolated worktree on a fresh `atlas/<id>` branch (your real branch
                 is never touched; you review the diff and merge).
  - non-git   -> a timestamped backup copy is taken first (rollback), then the session
                 runs in place.

It writes status + result back into the job JSON. Stdlib only — no yaml, no deps.

Start it alongside the dashboard:
    py -3.12 worker.py            # watch loop
    py -3.12 worker.py --once     # drain the queue once and exit (used for tests)
"""
import os
import re
import sys
import json
import time
import shutil
import difflib
import threading
import subprocess
import datetime as dt
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent          # D:\work\atlas\dashboard
ATLAS = HERE.parent                             # D:\work\atlas
JOBS = ATLAS / "jobs"
WORKTREES = ATLAS / "worktrees"
BACKUPS = ATLAS / "backups"
HEARTBEAT = JOBS / "_worker.json"        # liveness beacon the control center reads (NOT a job record)
HEARTBEAT_SECS = 5                       # how often the worker stamps the beacon
HEARTBEAT_STALE = 20                     # a beacon older than this => worker presumed dead
CLAUDE = shutil.which("claude") or os.environ.get("ATLAS_CLAUDE_BIN") or "claude"
JOB_TIMEOUT = int(os.environ.get("ATLAS_JOB_TIMEOUT", "3600"))   # seconds (1h default; per-session guard — override e.g. 7200 for a known-huge job)
KEEP_TERMINAL = int(os.environ.get("ATLAS_KEEP", "10"))          # finished records to retain
KEEP_WORKTREES = int(os.environ.get("ATLAS_KEEP_WORKTREES", "3"))  # finished worktrees to retain
REWORK_BUDGET = int(os.environ.get("ATLAS_REWORK_BUDGET", "2"))  # rework attempts (3 total tries)
PRUNABLE = {"done", "failed", "merged"}                          # ready stays (awaits merge)
DIFF_CAP = 60000                                                 # chars of diff shown to reviewer

# --- concurrency: the multi-worker GLOBAL engine (docs/agentic-structure.md §Multi-worker global engine) ---
# ONE worker process, concurrent ACROSS jobs. Two tracks = two pools in this one process:
#   LIGHT  — planner/architect/vault-keeper: one claude call, no worktree, no test suite. Its own
#            small pool so "ask the planner" is NEVER blocked by a heavy dispatch (the daily win).
#   HEAVY  — dispatch: sandboxed worktree + test gate. PART 2: up to MAX_JOBS run at once, gated by
#            the cross-session same-project overlap check + two GLOBAL permits below.
# The operator's knobs (MAX_JOBS × MAX_LANES declarable) sit on top of two hard machine ceilings:
#   MAX_WORKERS caps total concurrent claude sessions (cheap locally, but subscription-bound);
#   MAX_SUITES  caps total concurrent test suites (the real CPU/RAM hog). Both span cross-job AND
#   within-job lanes, so N jobs × M lanes can DECLARE 9 but never RUN more than the ceilings.
LIGHT_KINDS = {"architect", "planner", "vault-keeper"}
MAX_JOBS    = int(os.environ.get("ATLAS_MAX_JOBS", "3"))     # concurrent heavy dispatches
MAX_LIGHT   = int(os.environ.get("ATLAS_MAX_LIGHT", "3"))    # concurrent light control-plane jobs (planner+architect+vault-keeper can all run at once; +1 CLI ~0.2GB, no suite)
MAX_WORKERS = int(os.environ.get("ATLAS_MAX_WORKERS", "4"))  # GLOBAL concurrent claude sessions (heavy track)
MAX_SUITES  = int(os.environ.get("ATLAS_MAX_SUITES", "2"))   # GLOBAL concurrent test suites

# The two global permits. Acquired around the actual subprocess: a claude session (heavy track only —
# light jobs never take one, so the light track stays unblockable) and a test suite. Held per STEP,
# never per lane, so a lane testing/parked frees its claude permit for another lane.
CLAUDE_SEM = threading.BoundedSemaphore(max(1, MAX_WORKERS))
TEST_SEM   = threading.BoundedSemaphore(max(1, MAX_SUITES))


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def load(jid):
    return json.loads((JOBS / f"{jid}.json").read_text(encoding="utf-8"))


def save(job):
    (JOBS / f"{job['id']}.json").write_text(json.dumps(job, indent=2), encoding="utf-8")


# A dispatched session can't pause mid-run (it's a one-shot `claude -p`). Instead we give
# it a convention: when it hits a decision only the operator can make, it ends its turn with a
# NEEDS_INPUT: line. The worker detects that, parks the job as needs_input, and resumes the
# same session via `claude --resume` once the operator answers.
ESCALATION_DIRECTIVE = (
    "Escalation: you are running autonomously with no human in the loop. If — and only if — "
    "you hit a decision that only the operator (the owner) can make, or you are blocked and cannot "
    "proceed safely, do NOT guess. Stop, and end your reply with a single final line in "
    "exactly this form:\n"
    "NEEDS_INPUT: <your one-line question>\n"
    "Ask only when genuinely blocked; otherwise complete the task and do not emit that line."
)

# The session runs in a cwd-bounded sandbox. In a git worktree the real `.git` lives in the
# MAIN repo (outside cwd), so any `git` write op (commit/add/push) is denied — and the model
# can waste turns retrying it, sometimes ending with a misleading "I need permission to
# commit" that looks like success. Atlas's worker (unsandboxed) owns version control, so tell
# the session to just edit files and never run git.
SANDBOX_DIRECTIVE = (
    "Version control: do NOT run git (no commit, add, push, branch, stash). Atlas handles "
    "version control for you outside this session — just make the file changes in the working "
    "directory and describe them. Git commands will be denied and only waste turns."
)

_NEEDS_INPUT_RE = re.compile(r"^\s*NEEDS_INPUT:\s*(.+?)\s*$", re.MULTILINE)
# CONTRACT: / VERDICT: markers followed by a JSON object (may span lines → DOTALL).
_CONTRACT_RE = re.compile(r"^\s*CONTRACT:\s*(\{.*)", re.MULTILINE | re.DOTALL)
_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(\{.*)", re.MULTILINE | re.DOTALL)
_VERDICT_BARE_RE = re.compile(r"^\s*VERDICT:\s*(commit-ready|needs-changes|escalate)\b", re.MULTILINE)
VALID_VERDICTS = {"commit-ready", "needs-changes", "escalate"}


def detect_needs_input(result_text: str):
    """Return the escalation question if the session emitted a NEEDS_INPUT: line, else None.
    Uses the LAST occurrence, so a question in the final turn wins over any earlier mention."""
    matches = _NEEDS_INPUT_RE.findall(result_text or "")
    return matches[-1].strip() if matches else None


def detect_contract(result_text: str):
    """Return the implementation contract a scoping session emitted, else None.
    The session ends with `CONTRACT:` then a JSON object {brief, acceptance_criteria, test_cmd}."""
    m = _CONTRACT_RE.search(result_text or "")
    if not m:
        return None
    blob = m.group(1)
    a, b = blob.find("{"), blob.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        c = json.loads(blob[a:b + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(c, dict) or not str(c.get("brief", "")).strip():
        return None
    return {
        # the 2-line INTENT summary is the human gate: what gets built + what's out of scope.
        # Falls back to the first sentence of the brief if the scoper omitted it.
        "intent": str(c.get("intent", "")).strip() or str(c.get("brief", "")).strip()[:240],
        "brief": str(c.get("brief", "")).strip(),
        "acceptance_criteria": [str(x).strip() for x in (c.get("acceptance_criteria") or [])
                                if str(x).strip()],
        "test_cmd": (c.get("test_cmd") or None),
        # OPTIONAL: the single worker agent (an agents.yaml id) that will code a SERIAL job. Purely
        # for display — it lets the org view light the REAL worker during `coding` instead of pulsing
        # the whole worker tier. Engine never dispatches on it. A multi-step plan names agents per step.
        "agent": str(c.get("agent", "")).strip() or None,
        # OPTIONAL multi-worker plan. Absent/unusable → [] → the job runs as ONE lane, which is
        # exactly the pre-fan-out serial behaviour. Never infer a plan the scoper didn't declare.
        "plan": normalize_plan(c.get("plan")),
    }


def normalize_plan(raw):
    """Validate a scoper-emitted plan into an ordered list of worker steps.

    Returns [] for anything unusable — no plan, not a list, no step with a brief. [] is the
    signal for "one implicit lane" (today's serial path), so a malformed plan degrades to
    working-but-serial rather than failing the job.

    A step is {id, brief, paths, depends_on}. `paths` are the files/globs the step will touch —
    the orchestrator set-intersects them to decide what may run in parallel. `depends_on` names
    steps that must finish first.
    """
    if not isinstance(raw, list):
        return []
    steps, seen = [], set()
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        brief = str(s.get("brief", "")).strip()
        if not brief:                                  # a step with no work is not a step
            continue
        sid = str(s.get("id", "")).strip() or f"s{i + 1}"
        while sid in seen:                             # ids must be unique — they key the DAG
            sid = f"{sid}_{i + 1}"
        seen.add(sid)
        steps.append({
            "id": sid,
            "brief": brief,
            # optional: the worker agent (an agents.yaml id) this step belongs to. Purely for
            # display — it's what lets the org view light the REAL agent's parallel instances
            # instead of guessing. The engine never dispatches on it.
            "agent": str(s.get("agent", "")).strip() or None,
            "paths": [str(p).strip() for p in (s.get("paths") or []) if str(p).strip()],
            "depends_on": [str(d).strip() for d in (s.get("depends_on") or []) if str(d).strip()],
            # Per-step criteria. A lane's reviewer must be judged against ITS OWN definition of
            # done — handing it the whole session's criteria asks it to verify work that a
            # sibling lane is doing in parallel, which reads as a self-contradictory contract.
            "acceptance_criteria": [str(x).strip() for x in (s.get("acceptance_criteria") or [])
                                    if str(x).strip()],
        })
    # Drop dangling/self deps: a scoper typo naming a step that doesn't exist must not deadlock
    # the ready-set scheduler. A real cycle is caught separately (plan_cycle) and surfaced to the operator.
    ids = {s["id"] for s in steps}
    for s in steps:
        s["depends_on"] = [d for d in s["depends_on"] if d in ids and d != s["id"]]
    return steps


def plan_cycle(steps):
    """True if the plan's depends_on graph has a cycle (→ unschedulable; park for the operator rather
    than spin forever waiting for a step whose dependency can never complete)."""
    pending = {s["id"]: set(s["depends_on"]) for s in steps}
    while pending:
        ready = [sid for sid, deps in pending.items() if not deps]
        if not ready:
            return True
        for sid in ready:
            pending.pop(sid)
        for deps in pending.values():
            deps.difference_update(ready)
    return False


def scoping_directive(test_cmd):
    """System directive that turns a session into a read-only contract producer. It
    interrogates the operator (via the NEEDS_INPUT loop) then emits a single CONTRACT block."""
    suggestion = test_cmd or "null (this target has no automated tests — rely on the review gate)"
    return (
        "Scoping mode — you are NOT implementing anything. Your sole job is to produce an "
        "implementation CONTRACT for a downstream autonomous coding session. Read the codebase "
        "read-only as needed to scope the work. Interrogate the operator to resolve every ambiguity that "
        "affects scope, approach, or acceptance — ask ONE question per turn using the NEEDS_INPUT "
        "line. For a trivial, unambiguous task, ask nothing. When (and only when) the work is "
        "fully specified, end your reply with the contract as a single block, exactly:\n"
        "CONTRACT:\n"
        '{"intent": "<TWO short plain-English sentences a non-expert approves in seconds: first '
        "what will be built, then what is explicitly OUT of scope (<=35 words total). No jargon, "
        'no file lists — this is the human\'s approval gate. Valid JSON string (no raw newlines)>", '
        '"brief": "<a precise, self-contained brief for the coder: goal, scope, files to touch, '
        'and definition of done>", "acceptance_criteria": ["<objective, checkable item>", "..."], '
        '"test_cmd": "<command that verifies the work, or null>", '
        '"agent": "<the single worker agent id from this project\'s agents.yaml that will code this '
        'job — pick the one whose charter fits the work (e.g. design-engineer for UI, backend-engineer '
        'for an API change); omit if unsure. Leave OUT when you emit a multi-step plan — each step names '
        'its own agent instead. Display-only: it lets the control center light the real worker>", '
        '"plan": [{"id": "s1", "brief": "<self-contained brief for ONE worker>", '
        '"paths": ["<file or glob this step will touch>"], "depends_on": [], '
        '"agent": "<optional: the worker agent id from this project\'s agents.yaml that owns '
        'this step>", "acceptance_criteria": ["<objective, checkable item for THIS STEP ONLY>"]}]}\n'
        "When you emit a plan, SPLIT the acceptance_criteria across its steps: each step gets the "
        "criteria its own worker can satisfy, and the top-level list stays the whole session's. A "
        "step's reviewer sees only that step's diff, so a criterion belonging to a sibling step "
        "would read as a contract it cannot honour.\n"
        "The `plan` is OPTIONAL and exists for one purpose: work that splits into INDEPENDENT "
        "pieces runs as parallel workers, each in its own isolated worktree with its own test + "
        "review gate. Emit a multi-step plan ONLY when the pieces are genuinely separable (e.g. "
        "one category of findings per step); omit `plan` entirely — or give it a single step — for "
        "ordinary single-focus work, which is the common case. When you do emit one: `paths` must "
        "list every file or glob that step will touch (the orchestrator set-intersects them, and "
        "steps that overlap are run in sequence, never concurrently — so an incomplete `paths` risks "
        "two workers colliding), and `depends_on` names the step ids that must complete first. Each "
        "step's brief must stand alone: its worker sees only that brief, not the others.\n"
        f"Suggested test_cmd for this project: {suggestion}. IMPORTANT — the test_cmd is run by the "
        "gate in a FRESH, ISOLATED git worktree with NO pre-installed environment, so it MUST be "
        "self-contained: it has to set up its own dependencies via the project's declared tooling "
        "(e.g. `uv run pytest -q` for a uv/pyproject project, `npm ci && npm test` for node) rather "
        "than assuming an already-installed venv or globally-importable packages. The only things "
        "guaranteed on the machine are the language runtime + that tooling on PATH (git, python, uv, "
        "node as the stack requires). The `intent` is what the operator reads to approve; the brief/criteria "
        "are for the coder and the gate. Emit the CONTRACT block only once, when finalizing — never "
        "alongside a NEEDS_INPUT line."
    )


# --- architect (global agent): designs a project's agents.yaml + procedures.yaml ---
# Output convention (interactive: interrogate via NEEDS_INPUT, then ONE proposal block):
#   PROPOSAL
#   SUMMARY:
#   <2-4 sentence plain-language blurb — SHORT; the ladder goes in its own block>
#   --- ladder ---
#   <one line per automatable part: part — rung — validator>
#   --- agents.yaml ---
#   <yaml>
#   --- procedures.yaml ---
#   <yaml>
#   --- end ---
# The `--- ladder ---` block is OPTIONAL in the regex (older/simple proposals may omit it).
_PROPOSAL_RE = re.compile(
    r"SUMMARY:\s*\n(.*?)\n?(?:---\s*ladder\s*---\s*\n(.*?)\n?)?---\s*agents\.yaml\s*---\s*\n(.*?)\n?---\s*procedures\.yaml\s*---\s*\n(.*?)\n?---\s*end\s*---",
    re.DOTALL | re.IGNORECASE)


def _strip_fence(s: str) -> str:
    """Drop a wrapping ```yaml … ``` fence if the model added one."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def detect_proposal(result_text: str):
    """Return {summary, ladder, agents_yaml, procedures_yaml} if the architect emitted a PROPOSAL
    block (short summary on top + a SEPARATE ladder-classification block, §6a), else None. The
    ladder block is optional (older/simple proposals may omit it → empty string)."""
    m = _PROPOSAL_RE.search(result_text or "")
    if not m:
        return None
    summary = (m.group(1) or "").strip()
    ladder = (m.group(2) or "").strip()
    ag = _strip_fence(m.group(3))
    pr = _strip_fence(m.group(4))
    if not ag:
        return None
    return {"summary": summary, "ladder": ladder, "agents_yaml": ag, "procedures_yaml": pr}


def _read_atlas(rel) -> str:
    try:
        return (ATLAS / rel).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def architect_directive(job: dict) -> str:
    """Turn a session into the Atlas architect. SUBJECT-AWARE: it designs the org for a
    project (interrogate; read no project prose), a new Atlas capability (may read Atlas's own
    structural docs), OR an umbrella middle tier (reads the design-of-record + the current
    umbrella org and EXTENDS it). For any subject it ALSO classifies the subject's automatable
    parts on the automation ladder (docs/automation-patterns.md §5). Emits ONE PROPOSAL block;
    The operator approves. Selected by job['subject_kind'] ('project' default | 'capability' | 'umbrella')."""
    taxonomy = _read_atlas("agents.yaml")
    ex_ag = _read_atlas(Path("examples") / "demo-app" / "agents.yaml")
    ex_pr = _read_atlas(Path("examples") / "demo-app" / "procedures.yaml")
    capability = (job.get("subject_kind") or "project") == "capability"
    umbrella = (job.get("subject_kind") or "project") == "umbrella"

    if umbrella:
        subject = (
            "Your subject is an UMBRELLA — the MIDDLE tier of Atlas's org, between the brain "
            "(global agents) and each sub-project. You design the agents that work ACROSS all of "
            "the umbrella's sub-projects (never within one — those are each sub-project's own org). "
            "GROUND yourself FIRST by reading, in this repo: docs/agentic-structure.md (the design "
            "of record for the org tiers), CLAUDE.md, docs/automation-patterns.md (the ladder), and the "
            "CURRENT org files in your working directory — ./agents.yaml and ./procedures.yaml — "
            "because you EXTEND them (keep what's live, add what's missing), you do not replace them. "
            "For Orchard the intended set is exactly two: Ask Orchard (read-only vault lens, ALREADY "
            "LIVE — keep its entry as-is) and the vault-keeper (WRITE-authorized: substrate claude, "
            "model opus, effort high, rung 1.5, permissions {write:true, git:true}, verified_by: "
            "human; it owns the procedures update-vault, refresh-north-star, and "
            "propose-updates-from-a-merged-session). Your REAL deliverable is the reflection-heavy "
            "part the design doc leaves open: the vault-keeper's charter and the EXACT `instructions` "
            "for each of its procedures (a vault write is propose-only → human-gated; the "
            "deterministic vault-lint is its pre-check). Emit the COMPLETE agents.yaml (Ask Orchard + "
            "vault-keeper) and the COMPLETE procedures.yaml — approval overwrites the whole file. "
            "Confirm anything the design doc leaves ambiguous with the operator; do not invent scope."
        )
    elif capability:
        subject = (
            "Your subject is a NEW ATLAS CAPABILITY (a component of Atlas ITSELF), not a downstream "
            "project. To ground yourself you MAY read Atlas's own structural docs in this repo — "
            "CLAUDE.md (what Atlas is), docs/automation-patterns.md (the ladder you classify against), "
            "automation-map.yaml (Atlas's current self-model), and the capability's own design doc if "
            "the operator named one in the goal (e.g. docs/planner.md). Read those first, THEN "
            "interrogate the operator for anything still ambiguous. Add agents ONLY if the capability genuinely "
            "needs them — a static, rung-0-to-2 capability (compute + display) may need NONE; if so, "
            "say so explicitly and put a single explanatory comment line in the agents.yaml block (it "
            "must not be empty). Atlas capabilities are BUILT by Claude Code sessions, never by Atlas — "
            "you only DEFINE the org level."
        )
    else:
        subject = (
            "Your subject is ONE project. You read NO project prose (no PLAN.md, no README) — "
            "interrogate the operator for anything you need to know about scope, stack, or vision. Follow the "
            "Atlas role taxonomy and brain templates below: instantiate orchestrator/scoper/gatekeeper "
            "from the templates (tune their charters to this project), and add the worker(s) the work "
            "needs (soft cap 5). Every write-capable agent MUST declare a verifier (verified_by: "
            "<agent-id> | tests | human)."
        )

    return (
        "You are the Atlas ARCHITECT. Design the MINIMAL agent org + procedures for your subject — "
        "as small as it currently warrants. You do NOT implement anything and you do NOT create or "
        "edit any files; you only PROPOSE (as text).\n\n"
        + subject + "\n\n"
        "Procedures are REPEATABLE, never-changing operations (each references its owning agent by "
        "id); anything needing reflection is NOT a procedure.\n\n"
        "ALWAYS classify your subject on the automation ladder (docs/automation-patterns.md §5): for "
        "each automatable part, give its RUNG (0 deterministic / 1 one-shot / 1.5 one-shot+verify / "
        "2 loop / 2.5 fan-out / boundary) AND its VALIDATION METRIC (the concrete check that proves "
        "the output is right). Anything irreversible / financial / outward-facing is the never-automate "
        "BOUNDARY. A project mostly inherits the gate=2 / scoping=1.5 / merge=boundary profile plus "
        "anything project-specific; a capability or umbrella derives its profile fresh (e.g. the "
        "vault-keeper is rung 1.5 — one propose, vault-lint pre-check, human gate). This classification goes in "
        "its OWN '--- ladder ---' block, NOT in the summary; it will later be appended to "
        "automation-map.yaml.\n\n"
        "Interrogate the operator to resolve anything ambiguous — ask ONE question per turn via the NEEDS_INPUT "
        "line. When (and only when) the org is fully specified, output the proposal as a single block, "
        "EXACTLY in this form (valid YAML between the markers, NO code fences):\n"
        "PROPOSAL\nSUMMARY:\n<2-4 SHORT plain-language sentences ONLY — what the subject is + the "
        "headline decisions (e.g. 'no new agents; built by Stage-4 sessions'). A skim-and-approve blurb; "
        "keep it tight. Do NOT put the ladder list here.>\n"
        "--- ladder ---\n<one line per automatable part, formatted 'part — rung — validator'>\n"
        "--- agents.yaml ---\n<agents.yaml content, OR a single comment line if the subject needs no new "
        "agents>\n--- procedures.yaml ---\n<procedures.yaml content>\n--- end ---\n"
        "Never emit the PROPOSAL block alongside a NEEDS_INPUT line.\n\n"
        f"=== Atlas role taxonomy + brain templates (agents.yaml) ===\n{taxonomy}\n\n"
        f"=== Example project org (format reference) — agents.yaml ===\n{ex_ag}\n\n"
        f"=== Example project org (format reference) — procedures.yaml ===\n{ex_pr}\n"
    )


# --- vault-keeper (Orchard umbrella agent): drafts a gated change to the Orchard vault ---
# Output convention (interactive: interrogate via NEEDS_INPUT, else ONE proposal block):
#   PROPOSAL
#   SUMMARY:
#   <2-5 sentence change-summary — what changed X->Y and why>
#   --- files ---
#   <one line per file created / edited / superseded>
#   --- end ---
# The actual draft (git diff) + the deterministic vault-lint are captured by the worker, not the text.
_VK_PROPOSAL_RE = re.compile(
    r"SUMMARY:\s*\n(.*?)\n?(?:---\s*files\s*---\s*\n(.*?)\n?)?---\s*end\s*---",
    re.DOTALL | re.IGNORECASE)


def detect_vault_proposal(result_text: str):
    """Return {summary, files} if the vault-keeper emitted a PROPOSAL block, else None."""
    m = _VK_PROPOSAL_RE.search(result_text or "")
    if not m:
        return None
    summary = (m.group(1) or "").strip()
    if not summary:
        return None
    return {"summary": summary, "files": (m.group(2) or "").strip()}


def _vault_diff(root: str) -> str:
    """The vault-keeper's uncommitted draft as a readable diff across the WHOLE vault repo — wiki/,
    archive/, AND root docs (progress.md/log.md/…). This MUST match what approve commits (git add -A):
    the lint and the vault span the whole repo (os.walk minus dotdirs), so a wiki-only diff would hide
    edits the operator is about to commit. Enqueue refuses a dirty tree, so the entire delta IS the draft.
    Created docs show via intent-to-add. Capped so a big change doesn't bloat the job record."""
    import subprocess
    subprocess.run(["git", "-C", root, "add", "-N", "."], capture_output=True, text=True)   # created docs anywhere show in diff
    stat = subprocess.run(["git", "-C", root, "diff", "--stat"], capture_output=True, text=True).stdout or ""
    full = subprocess.run(["git", "-C", root, "diff"], capture_output=True, text=True).stdout or ""
    out = (stat.strip() + "\n\n" + full).strip()
    return out[:14000] + ("\n… (diff truncated)" if len(out) > 14000 else "")


def _vault_docs(root: str) -> list:
    """The files the vault-keeper touched — each with its git status, stem, AND its own diff — so the
    control center can LIST them, open each one RENDERED (via /api/vault/doc?id=<stem>, read live from disk),
    and show a per-file "what changed" toggle (the rendered doc shows the RESULT; the per-file diff
    shows the CHANGE — the only meaningful view for a deleted file). Spans the WHOLE repo (not just
    wiki/) so the list matches what approve commits; the vault reader (_iter_md) finds archive/ and
    root docs by stem too. Each diff is capped individually so no single big file starves the rest."""
    import subprocess
    subprocess.run(["git", "-C", root, "add", "-N", "."], capture_output=True, text=True)  # created docs anywhere show
    out = subprocess.run(["git", "-C", root, "diff", "--name-status"],
                         capture_output=True, text=True).stdout or ""
    docs = []
    for line in out.splitlines():
        parts = [p for p in line.split("\t") if p.strip()]
        if len(parts) < 2:
            continue
        status, rel = parts[0].strip()[:1].upper(), parts[-1].strip()   # A/M/D/R; renames → new path
        stem = rel.rsplit("/", 1)[-1]
        if stem.lower().endswith(".md"):
            stem = stem[:-3]
        fd = subprocess.run(["git", "-C", root, "diff", "--", rel], capture_output=True, text=True).stdout or ""
        fd = fd[:8000] + ("\n… (diff truncated)" if len(fd) > 8000 else "")
        docs.append({"rel": rel, "stem": stem, "status": status, "diff": fd.strip()})
    return docs


def _vault_lint() -> dict:
    """Run Atlas's deterministic vault-lint (vault.py:lint) over the current (edited) vault —
    the rung-1.5 gate. This is the whole reason the vault-keeper is allowed to hold a write
    permission: the check that accepts its draft is code, not another model. Never raises —
    a lint that crashed must read as a FAILED gate, never as a passed one."""
    try:
        import vault
        return vault.lint()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "issue_count": -1, "issues": []}


def _vault_revert(root: str):
    """Drop any uncommitted draft in the vault working tree (revert tracked edits + remove new
    files anywhere in the vault). Used on stop/failure so the next run starts clean. Whole-repo clean
    (not wiki/-only) so a doc the agent created in archive/ or the root can't be orphaned; enqueue
    guaranteed a clean tree, so only the draft is present. `-fd` (no -x) leaves gitignored files be."""
    import subprocess
    subprocess.run(["git", "-C", root, "reset", "-q"], capture_output=True, text=True)
    subprocess.run(["git", "-C", root, "checkout", "--", "."], capture_output=True, text=True)
    subprocess.run(["git", "-C", root, "clean", "-fd"], capture_output=True, text=True)


def vaultkeeper_directive(job: dict) -> str:
    """Turn a session into the Atlas VAULT-KEEPER: edit the Orchard vault IN PLACE per the operator's request,
    following the vault conventions + its own authored procedures, keep it lint-clean, then STOP and
    propose (never commit — Atlas commits on approval)."""
    procs = _read_atlas(Path("umbrellas") / "orchard" / "procedures.yaml")
    agents = _read_atlas(Path("umbrellas") / "orchard" / "agents.yaml")
    return (
        "You are the Atlas VAULT-KEEPER — the write-authorized steward of the the Orchard vault decision vault "
        "(the company-wide source of truth for every Orchard sub-project) and owner of the Orchard "
        "north-star. Your WORKING DIRECTORY IS the Orchard vault repo; the docs live under wiki/. "
        "Ground yourself in the vault's own CLAUDE.md (the vault's conventions) and your own authored "
        "procedures (below), then fulfil the operator's request by EDITING THE VAULT IN PLACE — create, edit, "
        "or supersede the right wiki/ files.\n\n"
        "YOU ARE PROPOSE-ONLY: make the edits and keep the vault valid, then STOP. DO NOT run git and "
        "DO NOT commit — Atlas captures your diff, runs the deterministic vault-lint, and commits ONLY "
        "after the operator approves.\n\n"
        "RULES:\n"
        "- Match the request to the right procedure: update-vault (record/revise a decision or "
        "concept), refresh-north-star (edit ONLY concept_orchard-northstar.md, preserving its "
        "mission:/guardrails: frontmatter), or a maintenance fix (e.g. resolving broken [[links]]). "
        "The full instructions are in the procedures.yaml below.\n"
        "- Conventions: `<type>_<slug>.md` naming; the title/type/tags/created/updated/sources "
        "frontmatter (reuse existing tags before inventing); `[[wikilink]]` cross-refs by filename "
        "stem; keep index.md + log.md current. SUPERSEDE (a ⛔ banner + [[successor]] link), never "
        "delete, a decision you override.\n"
        "- Keep the vault VALID: every [[link]] must resolve to a real doc, frontmatter must parse. "
        "Atlas runs a deterministic vault-lint over your result as the gate — a failing lint blocks "
        "the proposal, so resolve issues before you stop.\n"
        "- Scope your edits to EXACTLY what the request needs — nothing more.\n"
        "- If the request is ambiguous or strategically underspecified, ASK ONE question via a "
        "NEEDS_INPUT line instead of guessing — you are writing the company's source of truth.\n\n"
        "When the edits are done and the vault is valid, STOP and emit EXACTLY this block (no code "
        "fences):\n"
        "PROPOSAL\nSUMMARY:\n<2-5 plain sentences: what you changed X->Y and WHY — a change-summary, "
        "NOT a dump of the docs>\n--- files ---\n<one line per file you created / edited / superseded>\n"
        "--- end ---\n"
        "Never emit the PROPOSAL block alongside a NEEDS_INPUT line.\n\n"
        f"=== Your authored procedures (umbrellas/orchard/procedures.yaml) ===\n{procs}\n\n"
        f"=== Your agent definition (umbrellas/orchard/agents.yaml) ===\n{agents}\n"
    )


# --- planner (global agent): proposes/maintains a project's plan.yaml (docs/planner.md §6) ---
# Output convention (interactive: interrogate via NEEDS_INPUT, then ONE proposal block):
#   PROPOSAL
#   SUMMARY:
#   <2-5 sentence plain-language summary>
#   --- plan.yaml ---
#   <yaml>
#   --- end ---
_PLAN_PROPOSAL_RE = re.compile(
    r"SUMMARY:\s*\n(.*?)\n?---\s*plan\.yaml\s*---\s*\n(.*?)\n?---\s*end\s*---",
    re.DOTALL | re.IGNORECASE)

PLANNER_CHARTER = (
    "You are Atlas's planner — the operator's planning assistant for one project. You hold a complete, "
    "current understanding of the project's plan and progress and your job is to take the "
    "planning mental load off the operator. Given where the project is now, propose and maintain its "
    "milestone->task plan: define, date (optionally) and prioritize milestones (exactly ONE "
    "active); decompose work into roughly comparable-sized, individually dispatchable tasks; "
    "file loose backlog tasks under the right milestone; set the active milestone and park the "
    "rest. ESTIMATE every task you propose — the scheduling layer cannot place a task that has "
    "no estimate. Estimate by WHO DOES THE WORK: a `dispatch` task is executed by agents, so "
    "estimate the AGENT-execution time (short); a `manual` task is done by the operator himself, so "
    "estimate the HUMAN time. Set `remaining` equal to `estimate` on a task not yet started. "
    "Estimates are always presented AS estimates, never as fact — if you cannot estimate a task "
    "without guessing, ASK the operator rather than inventing a number. "
    "Reason about what is at risk and how to sequence the work, and surface it clearly and "
    "briefly. You read ONLY structured state — plan.yaml, status.yaml, job/merge history — NEVER "
    "a project's prose (no PLAN.md, no README). Anything you do not know, ASK the operator. You NEVER do "
    "project work and you NEVER mark tasks done (a deterministic hook does that on merge). You "
    "propose; the operator approves or gives feedback; Atlas writes plan.yaml."
)

PLAN_YAML_SCHEMA_REF = """
project: <id>

milestones:
  - id: <slug>
    title: "<title>"
    status: active            # active | next | later | done   (exactly ONE active)
    target_date: 2026-07-15   # OPTIONAL
    done_date:                # set when marked done

tasks:
  - id: <slug>
    title: "<title>"
    milestone: <milestone id> # OPTIONAL — omit for loose backlog
    type: dispatch            # dispatch (worker can do it) | manual (the operator does it)
    state: next               # done | in_progress | next | todo
    priority: high            # high | medium | low
    target_date: 2026-07-02   # OPTIONAL — the DEADLINE (due-by), NOT the scheduled day
    estimate: 4h              # effort — agent-execution time if `dispatch`, human time if
                              #   `manual`. A task without one CANNOT be scheduled: set it on
                              #   EVERY task you propose (ask the operator rather than guess).
    remaining: 4h             # effort left; starts EQUAL to estimate, burns down per session
    sessions:                 # OPTIONAL — one entry per scheduled slot of work; omit when unplanned
      - { id: sess_a1b2, done: true, merged_at: 2026-07-16 }
    done_date:                # ATLAS-OWNED (see below)
    dispatch: job_...         # ATLAS-OWNED (see below) — link to the Atlas job
    notes: ""

# ATLAS-OWNED FIELDS — `dispatch`, `sessions_done`, `review_pending`, `done_date` (planner.md §3a).
# These are machine bookkeeping: Atlas writes them on dispatch, on a completed session and on a
# merge. They are NOT yours to author. Leave them exactly as you found them, or omit them — Atlas
# re-asserts them from the file on disk when it writes your proposal, so an invented or dropped
# value is discarded either way. Never invent a `dispatch:` id: it is the key a merge matches on,
# and a wrong one advances the wrong task.
"""


def detect_plan(result_text: str):
    """Return {summary, plan_yaml} if the planner emitted a PROPOSAL block (summary-on-top,
    §6a), else None."""
    m = _PLAN_PROPOSAL_RE.search(result_text or "")
    if not m:
        return None
    summary = (m.group(1) or "").strip()
    py_ = _strip_fence(m.group(2))
    if not py_:
        return None
    return {"summary": summary, "plan_yaml": py_}


def _read_plan_yaml(cwd: str) -> str:
    try:
        return (Path(cwd) / "plan.yaml").read_text(encoding="utf-8")[:20000]
    except Exception:  # noqa: BLE001
        return ""


def _read_status_yaml(cwd: str) -> str:
    try:
        return (Path(cwd) / "status.yaml").read_text(encoding="utf-8")[:8000]
    except Exception:  # noqa: BLE001
        return ""


# --- the planner's SECOND capability: the scheduling assistant (docs/scheduling.md §7) ---
# Same agent, GLOBAL scope: it fits every project's ready tasks into the operator's free time and proposes
# Atlas's OWN schedule.yaml. Output convention mirrors the plan proposal, with its own marker.
_SCHED_PROPOSAL_RE = re.compile(
    r"SUMMARY:\s*\n(.*?)\n?---\s*schedule\.yaml\s*---\s*\n(.*?)\n?---\s*end\s*---",
    re.DOTALL | re.IGNORECASE)

SCHEDULER_CHARTER = (
    "You are Atlas's planner, in your SCHEDULING capability. Given every project's ready tasks "
    "(with estimates, deadlines and remaining effort) and the operator's availability, propose where each "
    "task is worked across his 6am-midnight days. NEVER place work on unavailable time. Fill the "
    "available time with SLICES — book a session up to the free capacity and leave the rest as "
    "`remaining` for another slot; never force a whole task into one block. Respect deadlines and "
    "priority, and clearly surface anything that cannot fit before its deadline. You propose a "
    "readable day/week summary; the operator approves, edits, or revises with you; Atlas writes "
    "schedule.yaml. You NEVER do project work, you NEVER write files, and you NEVER mark anything "
    "done. Anything you do not know, ASK the operator."
)

SCHEDULE_YAML_SCHEMA_REF = """
availability:            # COPY THE EXISTING availability THROUGH UNCHANGED — it is the operator's, not yours
  blocks:
    - { day: 2026-07-20, start: "14:00", end: "16:00" }
  full_days:
    - 2026-07-19

placements:              # ONE entry per work-SESSION. A task may have SEVERAL (that is a slice).
  - session: sess_<short unique slug>   # you invent this id; it must be unique across the file
    project: <project id>               # as in Atlas's registry
    task: <task id>                     # as in that project's plan.yaml
    day: 2026-07-20
    start: "10:00"
    end: "12:00"
"""


def _read_schedule_yaml() -> str:
    try:
        return (ATLAS / "schedule.yaml").read_text(encoding="utf-8")[:20000]
    except Exception:  # noqa: BLE001
        return ""


def _read_all_plans() -> str:
    """Every watched project's plan.yaml, concatenated with its project id — the ready work the
    scheduling assistant fits into the free time. Structured state only (no prose), and read-only.
    Projects without a plan_file (or whose file is missing) are simply skipped."""
    import yaml
    out = []
    try:
        reg = yaml.safe_load((ATLAS / "registry.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return ""
    for e in (reg.get("projects") or []):
        pf = e.get("plan_file")
        if not pf:
            continue
        try:
            txt = Path(pf).read_text(encoding="utf-8")[:12000]
        except Exception:  # noqa: BLE001 — no plan.yaml on disk yet → nothing to schedule for it
            continue
        out.append(f"=== project: {e.get('id')} — plan.yaml ===\n{txt}")
    return "\n\n".join(out)


def detect_schedule(result_text: str):
    """Return {summary, plan_yaml} if the scheduling assistant emitted a PROPOSAL block, else None.
    The payload key stays `plan_yaml` so the whole propose→confirm→approve path is shared."""
    m = _SCHED_PROPOSAL_RE.search(result_text or "")
    if not m:
        return None
    summary = (m.group(1) or "").strip()
    sy = _strip_fence(m.group(2))
    if not sy:
        return None
    return {"summary": summary, "plan_yaml": sy}


def scheduler_directive(job: dict) -> str:
    """Turn a session into the Atlas scheduling assistant: read the CURRENT schedule.yaml (the operator's
    availability + anything already placed) + EVERY project's plan.yaml, then propose the placements."""
    sched = _read_schedule_yaml()
    plans = _read_all_plans()
    today = _now()[:10]
    plans_block = (f"\n=== Every project's ready work ===\n{plans}\n" if plans else
                   "\n(No project has a plan.yaml yet — there is NOTHING to schedule. Say so via "
                   "NEEDS_INPUT rather than inventing work.)\n")
    return (
        SCHEDULER_CHARTER + "\n\n"
        f"Today is {today}. You do NOT implement anything and you do NOT create or edit any files; "
        "you only PROPOSE (as text). Interrogate the operator to resolve anything ambiguous — ask ONE "
        "question per turn via the NEEDS_INPUT line. When (and only when) the schedule is fully "
        "specified, output the proposal as a single block, EXACTLY in this form (valid YAML "
        "between the markers, NO code fences):\n"
        "PROPOSAL\nSUMMARY:\n<2-5 plain-language sentences the operator skims and approves — what is booked "
        "on which days, which tasks got sliced across slots, and ANYTHING that cannot fit before "
        "its deadline>\n--- schedule.yaml ---\n<full schedule.yaml content>\n--- end ---\n"
        "Never emit the PROPOSAL block alongside a NEEDS_INPUT line.\n\n"
        "HARD RULES:\n"
        "  1. Never place a session on unavailable time (an availability block or a full_day).\n"
        "  2. Copy the existing `availability` through UNCHANGED — it is the operator's, you only add "
        "`placements`.\n"
        "  3. Only schedule tasks that are NOT done and have effort left; schedule at most the "
        "task's `remaining` (or `estimate`) in total across its sessions.\n"
        "  4. SLICE: if a task's remaining effort exceeds a free opening, book a session for what "
        "fits and place the rest in later openings. Never overrun a slot to make a task fit.\n"
        "  5. Keep sessions inside 06:00-24:00, and never double-book the same time.\n"
        "  6. If something cannot fit before its deadline, still propose the best you can AND call "
        "it out explicitly in the SUMMARY — do not silently drop it.\n"
        "  7. Treat every life-event in the CURRENT schedule.yaml's `events:` list where `busy` is "
        "true OR `type` is `free` as unavailable time you must never schedule work onto — the same "
        "hard rule as an availability block or full_day.\n"
        "  8. NEVER emit or modify an `events:` section in your proposed schedule.yaml — life-events "
        "are the operator's, captured separately, and are preserved automatically; propose `availability` "
        "and `placements` only.\n\n"
        f"=== schedule.yaml schema reference ===\n{SCHEDULE_YAML_SCHEMA_REF}\n"
        f"\n=== Atlas's CURRENT schedule.yaml ===\n{sched}\n"
        f"{plans_block}"
    )


# --- the planner's THIRD capability: re-estimate (docs/scheduling.md §7/§6) ---
# Same agent, per-task scope: given one task's current numbers + the operator's free-text note, propose a
# single revised `remaining`. Output convention mirrors the other two, with its own marker.
_REESTIMATE_PROPOSAL_RE = re.compile(
    r"SUMMARY:\s*\n(.*?)\n?---\s*remaining\s*---\s*\n(.*?)\n?---\s*end\s*---",
    re.DOTALL | re.IGNORECASE)

REESTIMATE_CHARTER = (
    "You are Atlas's planner, in your RE-ESTIMATE capability. The operator is mid-flight on one task and "
    "is reviewing its remaining effort during a remainder review. Given the task's title, original "
    "estimate, current `remaining`, which of its scheduled sessions are already done, and the operator's "
    "free-text note, propose a single revised `remaining` value (a short duration string like "
    "'3h' or '90m') with 1-2 lines of reasoning. You NEVER do project work, you NEVER write files, "
    "and you NEVER mark anything done. If the operator's note is ambiguous or you need a number he hasn't "
    "given, ASK via NEEDS_INPUT rather than guessing."
)


def detect_reestimate(result_text: str):
    """Return {summary, remaining} if the planner emitted a re-estimate PROPOSAL block, else None."""
    m = _REESTIMATE_PROPOSAL_RE.search(result_text or "")
    if not m:
        return None
    summary = (m.group(1) or "").strip()
    remaining = (m.group(2) or "").strip()
    if not remaining:
        return None
    return {"summary": summary, "remaining": remaining}


def _read_task_block(cwd: str, task_id: str) -> str:
    """The raw plan.yaml task block for `task_id` (read-only context for the re-estimate
    directive) — falls back to '' if the task/file can't be found; the directive handles that."""
    try:
        text = (Path(cwd) / "plan.yaml").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"^tasks:\s*\n", text, re.MULTILINE)
    if not m:
        return ""
    tasks_text = text[m.end():]
    block_re = re.compile(r"[ \t]*-\s*id:\s*\S.*?(?=\n[ \t]*-\s*id:|\n\S|\Z)", re.DOTALL)
    for bm in block_re.finditer(tasks_text):
        block = bm.group(0)
        if re.search(rf"^\s*-?\s*id:\s*{re.escape(task_id)}\s*$", block, re.MULTILINE):
            return block.strip()
    return ""


def reestimate_directive(job: dict) -> str:
    """Turn a session into the Atlas re-estimate assistant: read ONE task's current numbers from
    plan.yaml (estimate, remaining, sessions_done) + the operator's free-text note (sent separately as the
    turn's prompt), then propose a revised `remaining` for that task only."""
    cwd = job.get("cwd", "")
    task_id = job.get("task_id") or ""
    task_block = _read_task_block(cwd, task_id)
    task_block_text = (f"\n=== The task being reviewed (plan.yaml, id: {task_id}) ===\n{task_block}\n"
                        if task_block else
                        f"\n(Could not find task {task_id!r} in this project's plan.yaml — ask the operator "
                        "for the numbers via NEEDS_INPUT rather than inventing them.)\n")
    return (
        REESTIMATE_CHARTER + "\n\n"
        "You do NOT implement anything and you do NOT create or edit any files; you only PROPOSE "
        "(as text). If anything is ambiguous, ask ONE question per turn via the NEEDS_INPUT line. "
        "When (and only when) you have a single revised `remaining` value, output the proposal as "
        "a single block, EXACTLY in this form (NO code fences):\n"
        "PROPOSAL\nSUMMARY:\n<1-2 plain-language sentences: the revised remaining and why>\n"
        "--- remaining ---\n<the revised remaining, e.g. 3h>\n--- end ---\n"
        "Never emit the PROPOSAL block alongside a NEEDS_INPUT line.\n"
        f"{task_block_text}"
    )


def planner_directive(job: dict) -> str:
    """Turn a session into the Atlas planner: read the CURRENT plan.yaml (if any) + the
    project's status.yaml (NOT PLAN.md, NOT any prose), interrogate the operator for anything
    unknown, then emit ONE PROPOSAL block with a SUMMARY + the full plan.yaml."""
    cwd = job.get("cwd", "")
    plan = _read_plan_yaml(cwd)
    status = _read_status_yaml(cwd)
    plan_block = (f"\n=== This project's CURRENT plan.yaml ===\n{plan}\n" if plan else
                  "\n(This project has no plan.yaml yet — you are proposing the first one, "
                  "from scratch.)\n")
    status_block = (f"\n=== This project's status.yaml ===\n{status}\n" if status else
                    "\n(This project has no status.yaml.)\n")
    return (
        PLANNER_CHARTER + "\n\n"
        "You do NOT implement anything and you do NOT create or edit any files; you only "
        "PROPOSE (as text). Interrogate the operator to resolve anything ambiguous — ask ONE question "
        "per turn via the NEEDS_INPUT line. When (and only when) the plan is fully specified, "
        "output the proposal as a single block, EXACTLY in this form (valid YAML between the "
        "markers, NO code fences):\n"
        "PROPOSAL\nSUMMARY:\n<2-5 plain-language sentences the operator skims and approves — milestones, "
        "the active one, task counts, dates, top priority, anything at risk>\n--- plan.yaml ---\n"
        "<full plan.yaml content>\n--- end ---\n"
        "Never emit the PROPOSAL block alongside a NEEDS_INPUT line.\n\n"
        f"=== plan.yaml schema reference ===\n{PLAN_YAML_SCHEMA_REF}\n"
        f"{plan_block}"
        f"{status_block}"
    )


REVIEW_DIRECTIVE = (
    "You are an independent code REVIEWER for an autonomous dispatch system. A coding session "
    "produced a diff to satisfy a contract. You are read-only — do NOT edit any files or run git. "
    "Judge the diff against the contract on three axes:\n"
    "  (1) SCOPE — it does exactly what the brief asked, with no unrelated or out-of-scope edits;\n"
    "  (2) ACCEPTANCE — every acceptance criterion is actually satisfied by the diff itself (the "
    "objective test result is given to you; do not take the coder's word for anything);\n"
    "  (3) QUALITY/SAFETY — no obvious bugs, leftover debug/print/TODO debris, secrets, or breaks "
    "from the surrounding style.\n"
    "Be STRICT on correctness, scope, and safety; do NOT block on subjective style preferences. "
    "End your reply with EXACTLY one verdict block:\n"
    "VERDICT:\n"
    '{"verdict": "commit-ready" | "needs-changes" | "escalate", "summary": "<one line>", '
    '"findings": ["<specific, actionable fix>", "..."]}\n'
    "Use `needs-changes` for issues the coder can fix on its own (list each in findings). Use "
    "`escalate` when the problem is scope/intent/ambiguity or a risk only the owner can resolve. "
    "Use `commit-ready` only if you would merge it as-is. Emit the VERDICT block once, at the end."
)


def detect_verdict(result_text: str):
    """Parse the reviewer's verdict. Returns {verdict, summary, findings} or None (→ caller
    treats None as escalate: never auto-commit on an unparseable judgment)."""
    m = _VERDICT_RE.search(result_text or "")
    if not m:
        bare = _VERDICT_BARE_RE.search(result_text or "")
        return {"verdict": bare.group(1), "summary": "", "findings": []} if bare else None
    blob = m.group(1)
    a, b = blob.find("{"), blob.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        c = json.loads(blob[a:b + 1])
    except Exception:  # noqa: BLE001
        return None
    v = str(c.get("verdict", "")).strip()
    if v not in VALID_VERDICTS:
        return None
    return {
        "verdict": v,
        "summary": str(c.get("summary", "")).strip(),
        "findings": [str(x).strip() for x in (c.get("findings") or []) if str(x).strip()],
    }


def parse_result(stdout: str):
    """claude --output-format json prints a single JSON object.

    Every envelope that passes through here also has its session id recorded (docs/brain.md
    §14.10): a Claude Code session can write a memory file, and brain-keeper must be able to tell
    Atlas's own sessions from the operator's terminal ones. Dispatched work has never written one, so this
    is insurance rather than a fix, and it costs a dict lookup.
    """
    s = (stdout or "").strip()
    try:
        return _note_session(json.loads(s))
    except Exception:  # noqa: BLE001
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            try:
                return _note_session(json.loads(s[a:b + 1]))
            except Exception:  # noqa: BLE001
                return None
    return None


def _note_session(data):
    """Record the CLI session id, then hand the envelope back untouched. Never raises: this is
    bookkeeping for brain-keeper and must not affect a dispatch."""
    try:
        import atlas_sessions
        atlas_sessions.record((data or {}).get("session_id"))
    except Exception:  # noqa: BLE001
        pass
    return data


def prepare_sandbox(job: dict) -> str:
    """Returns the directory Claude should run in. Records the sandbox/branch on the job."""
    cwd = job["cwd"]
    if job.get("is_git"):
        WORKTREES.mkdir(exist_ok=True)
        wt = str(WORKTREES / job["id"])
        branch = f"atlas/{job['id']}"
        subprocess.run(["git", "-C", cwd, "worktree", "add", "-b", branch, wt, "HEAD"],
                       check=True, capture_output=True, text=True)
        job["sandbox"], job["branch"] = wt, branch
        return wt
    # read-only procedure: it changes nothing, so no backup is needed — just run in place.
    if job.get("read_only"):
        return cwd
    # non-git vault: snapshot first, then run in place
    BACKUPS.mkdir(exist_ok=True)
    bdir = str(BACKUPS / f"{job['project']}_{dt.datetime.now():%Y%m%d_%H%M%S}")
    shutil.copytree(cwd, bdir, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".venv", "node_modules"))
    job["sandbox"] = bdir
    return cwd


def diffstat_since(wt: str, base: str):
    """Diffstat of a worktree against the commit it branched from.

    The serial gate measures uncommitted work (`git diff HEAD`), but the JOIN's changes arrive
    as COMMITS — the lane merges — so by the time it reports, `diff HEAD` is empty and the
    session footer showed "(no file changes)" for a fan-out that had changed plenty.
    """
    if not (wt and base):
        return None
    r = subprocess.run(["git", "-C", wt, "diff", "--stat", base, "HEAD"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip() or "(no file changes)"


def diffstat(job: dict):
    if not job.get("is_git") or not job.get("sandbox"):
        return None
    wt = job["sandbox"]
    # The dispatched session can't git-add (sandbox blocks writes to the parent .git), so new
    # files are untracked and wouldn't show in `diff HEAD`. The worker isn't sandboxed, so we
    # mark intent-to-add first to surface them in the review diff.
    subprocess.run(["git", "-C", wt, "add", "-A", "-N"], capture_output=True, text=True)
    r = subprocess.run(["git", "-C", wt, "diff", "--stat", "HEAD"],
                       capture_output=True, text=True)
    return (r.stdout or "").strip() or "(no file changes)"


EFFORT_DIRECTIVE = {
    "low": "Effort: LOW. Make the minimal correct change that satisfies the brief. Do not "
           "explore or refactor beyond it; be economical with tool calls and tokens.",
    "medium": "Effort: MEDIUM. Balance thoroughness with economy. Stay scoped to the brief.",
    "high": "Effort: HIGH. Think carefully and work thoroughly; verify your work (run tests "
            "where relevant) before finishing. Stay within the brief's scope.",
}


# --- stop support: a running job can be aborted mid-subprocess via a jobs\<id>.stop marker ---
# The worker now runs several jobs at once (a heavy dispatch + the light control-plane track, and
# a fanned job's lanes each in their own thread), so "which job is THIS subprocess for" must be
# PER-THREAD, not one global. A thread-local carries the running thread's jid for the stop check;
# a ref-counted registry (many lane threads can share one jid) backs the heartbeat's active list.
_ACTIVE = threading.local()
_ACTIVE_JIDS: dict = {}                 # jid -> live-thread count, for the heartbeat beacon
_ACTIVE_LOCK = threading.Lock()


def _enter_active(jid):
    """Mark this thread as running `jid` (arms the stop signal for its subprocesses)."""
    _ACTIVE.jid = jid
    with _ACTIVE_LOCK:
        _ACTIVE_JIDS[jid] = _ACTIVE_JIDS.get(jid, 0) + 1


def _exit_active(jid):
    _ACTIVE.jid = None
    with _ACTIVE_LOCK:
        n = _ACTIVE_JIDS.get(jid, 0) - 1
        if n <= 0:
            _ACTIVE_JIDS.pop(jid, None)
        else:
            _ACTIVE_JIDS[jid] = n


def _current_jid():
    """The jid the CURRENT thread is running (None if idle) — what the stop check keys off."""
    return getattr(_ACTIVE, "jid", None)


def _active_jids():
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE_JIDS)


class JobStopped(Exception):
    """Raised inside the subprocess wait when a stop marker appears for the active job."""


def _stop_requested(jid):
    return bool(jid) and (JOBS / f"{jid}.stop").exists()


def _clear_stop(jid):
    try:
        (JOBS / f"{jid}.stop").unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


def _kill_tree(pid):
    """Kill a process AND its children (claude spawns helpers). Windows: taskkill /T."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, text=True)
    except Exception:  # noqa: BLE001 — best-effort; fall back to a direct kill
        try:
            os.kill(pid, 9)
        except Exception:  # noqa: BLE001
            pass


def _run_interruptible(cmd, cwd, shell=False, input_text=None):
    """Run a subprocess while polling for the active job's stop marker, draining stdout/stderr
    via reader threads so a chatty child never deadlocks on a full pipe. `input_text` is fed to
    stdin (used for the prompt, so a huge diff/brief never hits Windows' ~32KB cmdline limit).
    Returns a CompletedProcess; raises JobStopped on stop, TimeoutExpired at JOB_TIMEOUT."""
    import threading
    proc = subprocess.Popen(cmd, cwd=cwd, shell=shell,
                            stdin=(subprocess.PIPE if input_text is not None else None),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace", text=True)
    if input_text is not None:                  # feed the prompt via stdin on its own thread so a
        def _feed():                            # large payload never blocks on a full stdin pipe
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_feed, daemon=True).start()
    out, err = [], []
    to = threading.Thread(target=lambda: out.append(proc.stdout.read() or ""), daemon=True)
    te = threading.Thread(target=lambda: err.append(proc.stderr.read() or ""), daemon=True)
    to.start()
    te.start()
    start = time.time()
    while proc.poll() is None:
        if _stop_requested(_current_jid()):
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            raise JobStopped()
        if time.time() - start > JOB_TIMEOUT:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            raise subprocess.TimeoutExpired(cmd, JOB_TIMEOUT)
        time.sleep(1)
    to.join(timeout=5)
    te.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out), "".join(err))


def run_claude(task: str, run_dir: str, mode: str, model=None, effort=None, resume=None,
               extra=None, escalation=True, heavy=True):
    # `heavy` (default True) => acquire a global CLAUDE_SEM permit for the duration of the session,
    # so total concurrent claude sessions across ALL heavy jobs and their lanes never exceeds
    # MAX_WORKERS. Light control-plane jobs pass heavy=False: they're capped by their own MAX_LIGHT
    # pool instead, and must NOT contend for the heavy permit or the light track could be starved.
    # The prompt is fed via STDIN (see the return), not argv — a large diff/brief would otherwise
    # blow past Windows' ~32KB command-line limit (CreateProcess error 206) and fail to launch.
    cmd = [CLAUDE, "-p", "--output-format", "json"]
    if resume:                              # continue the parked session in the same sandbox
        cmd += ["--resume", resume]
    if model:
        cmd += ["--model", model]
    # The reviewer judges via a VERDICT line, not NEEDS_INPUT, so it drops the escalation bit.
    sys_bits = ([ESCALATION_DIRECTIVE] if escalation else []) + [SANDBOX_DIRECTIVE]
    if effort in EFFORT_DIRECTIVE:
        sys_bits.insert(0, EFFORT_DIRECTIVE[effort])
    if extra:                               # e.g. the scoping / review directive — leads the prompt
        sys_bits = list(extra) + sys_bits
    cmd += ["--append-system-prompt", "\n\n".join(sys_bits)]
    if mode == "skip":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd += ["--permission-mode", mode]
    if heavy:
        with CLAUDE_SEM:                    # global heavy-claude-session permit (MAX_WORKERS)
            return _run_interruptible(cmd, run_dir, input_text=task)
    return _run_interruptible(cmd, run_dir, input_text=task)


def _write_log(jid: str, proc, resuming: bool):
    entry = (proc.stdout or "") + "\n----- STDERR -----\n" + (proc.stderr or "")
    path = JOBS / f"{jid}.log"
    if resuming and path.exists():          # keep the original run; append the resume turn
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n===== RESUME @ {_now()} =====\n{entry}")
    else:
        path.write_text(entry, encoding="utf-8")


def _add_tokens(job: dict, data):
    u = (data or {}).get("usage") or {}
    # `tokens` = FRESH input+output only — the subscription-relevant figure, matching
    # dashboard/usage.py (the 7-day meter). Cache reads/creation are re-reads of the same
    # cached context (re-sent every turn) and dwarf the real number 10-80x, so they are
    # tracked separately in `cache_tokens` (informational), never in the headline count.
    fresh = int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
    cache = int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
    job["tokens"] = (job.get("tokens") or 0) + fresh
    job["cache_tokens"] = (job.get("cache_tokens") or 0) + cache


def _log_append(jid: str, section: str, body: str):
    with (JOBS / f"{jid}.log").open("a", encoding="utf-8") as fh:
        fh.write(f"\n\n===== {section} @ {_now()} =====\n{body}")


def _gate_invoke(job, label, task, model, effort, resume=None, extra=None, escalation=True):
    """Run a claude call inside the gate: logs it, accumulates tokens, returns (ok, text, data).
    Does NOT touch job['session_id'] — callers decide (rework keeps the coder session, the
    reviewer is a throwaway session)."""
    proc = run_claude(task, job["sandbox"], "skip", model=model, effort=effort,
                      resume=resume, extra=extra, escalation=escalation)
    _log_append(job["id"], label, (proc.stdout or "") + "\n----- STDERR -----\n" + (proc.stderr or ""))
    data = parse_result(proc.stdout)
    ok = proc.returncode == 0 and bool(data) and not data.get("is_error")
    _add_tokens(job, data)
    return ok, ((data or {}).get("result") or ""), data


def run_tests(job):
    """Run the project's test_cmd in the worktree (unsandboxed). Returns (passed, tail):
    passed is True/False, or None when there's no test_cmd (gate skips to review)."""
    cmd = job.get("test_cmd")
    if not cmd:
        return None, ""
    wt = job.get("sandbox") or job["cwd"]
    try:
        with TEST_SEM:                      # global test-suite permit (MAX_SUITES) — the real hog
            r = _run_interruptible(cmd, wt, shell=True)
    except subprocess.TimeoutExpired:
        return False, f"(test_cmd timed out after {JOB_TIMEOUT}s)"
    out = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
    _log_append(job["id"], f"TESTS (exit {r.returncode})", out[-6000:])
    return (r.returncode == 0), out[-4000:]


def get_diff(job):
    """Full `git diff HEAD` of the worktree (intent-to-add surfaces new files), capped for the
    reviewer prompt. None when there's no usable git diff (non-git vault)."""
    wt = job.get("sandbox")
    if not (job.get("is_git") and wt):
        return None
    subprocess.run(["git", "-C", wt, "add", "-A", "-N"], capture_output=True, text=True)
    r = subprocess.run(["git", "-C", wt, "diff", "HEAD"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    d = (r.stdout or "").strip()
    if not d:
        return "(no changes)"
    return d if len(d) <= DIFF_CAP else d[:DIFF_CAP] + f"\n...(diff truncated at {DIFF_CAP} chars)..."


# The evaluative core of the design-taste-frontend skill, distilled to the checks that catch
# AI-slop. Shared by the reviewer (judge) and coder (build) injections below — "invoke skill X" in a
# charter is a soft instruction LLMs follow unreliably, so this baseline is put in context reliably.
_TASTE_STANDARDS = (
    "- Anti-default: none of the AI-slop tells — AI-purple gradients, centered hero on a dark mesh, "
    "three identical feature cards, blanket glassmorphism, Inter+slate for everything.\n"
    "- Hierarchy: a clear type scale and one confident focal point per view; deliberate contrast of "
    "size/weight/space, not uniform greyness.\n"
    "- Restraint: a consistent spacing rhythm, a single accent used sparingly, no ornament for its "
    "own sake.\n"
    "- Consistency locks: one type scale, one accent colour, one radius/shape system used throughout "
    "— no section drifts off-system.\n"
    "- Contrast: body, heading, and UI text each meet WCAG AA on their background.\n"
    "- Decisively designed: reads as intentional, not a lightly-restyled template.\n"
)

# Injected DETERMINISTICALLY into the REVIEW prompt for any UI change, so the reviewer judges the
# rendered result against taste standards even if it never invokes the skill itself.
TASTE_RUBRIC = (
    "## Design standards — judge the LOOK, not just that it renders\n"
    "This change touches UI/presentation. In addition to the acceptance criteria, assess the "
    "captured Playwright screenshots against these anti-slop standards. Do NOT redesign — judge, "
    "and list any miss as a finding:\n"
    + _TASTE_STANDARDS
)

# The same standards, framed for the CODER: injected into the design worker's CODING brief so it
# BUILDS to them from the start (the full skill stays available in-repo for depth — this is the
# always-in-context baseline, not a replacement for invoking it).
TASTE_RUBRIC_BUILD = (
    "## Design standards — build to these, don't just make it render\n"
    "This is a UI/presentation task. Beyond the acceptance criteria, hold your work to these "
    "anti-slop standards as you build (the design-taste-frontend skill is installed in this repo — "
    "invoke it for the full guidance):\n"
    + _TASTE_STANDARDS
)

# Changed files that mark a review as a DESIGN review (→ inject TASTE_RUBRIC). Matches the diff's
# file headers, so it's fast and needs no filesystem walk.
_UI_FILE_RE = re.compile(
    r'^(?:diff --git a/|\+\+\+ b/).*\.(?:css|scss|sass|less|astro|tsx|jsx|vue|svelte)\b',
    re.MULTILINE)


def _is_design_review(job, diff) -> bool:
    """True when this diff is a UI/presentation change → the reviewer gets the taste rubric."""
    return bool(diff and _UI_FILE_RE.search(diff))


def _is_design_job(job, lane=None) -> bool:
    """True when the worker chosen to CODE this job is a design/UI agent → inject the BUILD rubric.
    No diff exists yet at coding-start, so (unlike the reviewer's file-header match) this keys off the
    worker the scoper recorded — the lane's step agent, else the contract's top-level agent (§4b)."""
    agent = (lane or {}).get("agent") or (job.get("contract") or {}).get("agent") or ""
    return "design" in str(agent).lower()


def run_reviewer(job, diff, test_summary):
    """Spawn a separate read-only reviewer session; return its parsed verdict (or None)."""
    c = job.get("contract") or {}
    # A LANE carries its own acceptance_criteria and must win over the session-wide contract's —
    # otherwise a fanned lane's reviewer is asked to verify a sibling lane's work against a diff
    # that (correctly) doesn't contain it, and reads the contract as self-contradictory.
    crit = job.get("acceptance_criteria") or c.get("acceptance_criteria") or []
    crit_txt = "\n".join(f"- {x}" for x in crit) or "(none specified)"
    # Belt-and-braces for a lane whose step didn't declare its own criteria: say plainly that
    # this diff is one step of a parallel plan, so a sibling's untouched work is not a finding.
    scope_txt = ""
    if job.get("lane_id") and not job.get("implicit"):
        scope_txt = (
            f"\n## Scope of THIS review\nThis diff is ONE step (`{job['lane_id']}`) of a multi-step "
            "plan whose steps are being implemented IN PARALLEL by separate workers, each in its own "
            "worktree. Judge it ONLY against the brief and criteria above. Work belonging to another "
            "step is out of scope: its absence from this diff is expected and is NOT a finding.\n")
    taste_txt = ("\n" + TASTE_RUBRIC) if _is_design_review(job, diff) else ""
    prompt = (
        "Review this dispatched change against its contract and return your VERDICT.\n\n"
        f"## Brief\n{job.get('task', '')}\n\n"
        f"## Acceptance criteria\n{crit_txt}\n"
        f"{taste_txt}"
        f"{scope_txt}\n"
        f"## Objective test result\n{test_summary}\n\n"
        f"## Diff (git diff HEAD)\n```diff\n{diff}\n```\n"
    )
    ok, text, _ = _gate_invoke(job, "REVIEW", prompt, job.get("review_model", "sonnet"),
                               job.get("review_effort", "high"), extra=[REVIEW_DIRECTIVE],
                               escalation=False)
    return detect_verdict(text) if ok else None


def build_rework_message(summary, findings):
    lines = ["A review gate found issues with your change. Fix them now, staying within the "
             "original contract. Do NOT run git — Atlas commits for you. When done, stop."]
    if summary:
        lines.append(f"Summary: {summary}")
    if findings:
        lines.append("Findings to address:")
        lines += [f"- {f}" for f in findings]
    return "\n".join(lines)


def do_commit(job, summary):
    """Commit the worktree changes on the disposable atlas/<id> branch (worker is unsandboxed,
    so unlike the dispatched session it CAN run git). Records the diffstat (pre-commit) + sha."""
    wt = job["sandbox"]
    job["diffstat"] = diffstat(job)         # capture before commit (diff HEAD goes empty after)
    subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, text=True)
    msg = (f"atlas: {(job.get('task') or '').splitlines()[0][:60]}\n\n"
           f"Dispatched + gated by Atlas job {job['id']}.\n{summary}".strip())
    subprocess.run(["git", "-C", wt, "commit", "-m", msg], capture_output=True, text=True)
    sha = subprocess.run(["git", "-C", wt, "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    job["commit"] = sha


def _set_yaml_scalar(text: str, key: str, value: str) -> str:
    """Set a top-level scalar `key: value` in a YAML doc via a targeted line edit, so comments
    and the rest of the file survive (we never round-trip the whole doc through a YAML dumper)."""
    v = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    line = f"{key}: {v}"
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pat.search(text):
        return pat.sub(lambda _m: line, text, count=1)
    return text.rstrip("\n") + f"\n{line}\n"


def _update_status_after_merge(job):
    """After a merge, bump the project's status.yaml: `updated`, then run the deterministic
    plan-advance hook (below) — `next_target` is derived from the plan when one exists.

    `last_session` is deliberately NOT written here. It is a JUDGMENT field (CLAUDE.md, inputs vs
    outputs) owned by the scribe's session-end procedure — every project's procedures.yaml already
    says this hook owns `updated` + `next_target` and nothing else, so writing it here was the hook
    reaching past its own declared contract. It clobbered a considered summary with a commit subject
    truncated at 80 chars, and a merge is not a session anyway: a day routinely lands several, and
    session-end covers them as one arc."""
    try:
        sf = os.path.join(job.get("sidecar_dir") or job["cwd"], "status.yaml")
        if not os.path.exists(sf):
            return
        with open(sf, encoding="utf-8") as fh:
            text = fh.read()
        today = dt.date.today().isoformat()
        text = _set_yaml_scalar(text, "updated", today)
        with open(sf, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception as e:  # noqa: BLE001 — status bump is best-effort, never fail the merge
        print(f"  ! status.yaml bump skipped: {e}")
    _advance_plan_after_merge(job)


# --- deterministic merge-advance hook (docs/planner.md §6.6 — plain code, NO LLM) ---
# plan.yaml's `tasks:` list is a sequence of small mappings; we edit it with targeted line
# surgery (same spirit as _set_yaml_scalar) rather than a full YAML round-trip, so comments
# and formatting elsewhere in the file survive and we stay stdlib-only (no yaml dep here).
_TASK_BLOCK_RE = re.compile(
    r"(?P<indent>[ \t]*)-\s*id:\s*\S.*?(?=\n[ \t]*-\s*id:|\n\S|\Z)", re.DOTALL)


def _sessions_done_ids(block: str) -> list:
    """The task's already-closed session ids. Mirrors dispatcher._sessions_done_list — Atlas writes
    this field flow-style (`[sess_a, sess_b]`) from both the UI and this hook."""
    cur = _task_field(block, "sessions_done")
    if cur and cur.strip().startswith("["):
        return [x.strip() for x in cur.strip()[1:-1].split(",") if x.strip()]
    return []


def _find_task_block(tasks_text: str, jid: str):
    """Return (start, end, block) of the task item whose `dispatch:` equals jid, else None."""
    for m in _TASK_BLOCK_RE.finditer(tasks_text):
        block = m.group(0)
        if re.search(rf"^\s*dispatch:\s*[\"']?{re.escape(jid)}[\"']?\s*$", block, re.MULTILINE):
            return m.start(), m.end(), block
    return None


def _set_block_field(block: str, key: str, value: str, indent: str) -> str:
    """Set (or insert) a `key: value` scalar line inside a task block, preserving the rest."""
    line = f'{indent}  {key}: "{value}"'
    pat = re.compile(rf"^{indent}  {re.escape(key)}:.*$", re.MULTILINE)
    if pat.search(block):
        return pat.sub(lambda _m: line, block, count=1)
    return block.rstrip("\n") + f"\n{line}"


def _task_field(block: str, key: str):
    m = re.search(rf"^\s*-?\s*{re.escape(key)}:\s*(.+?)\s*$", block, re.MULTILINE)
    if not m:
        return None
    v = m.group(1).strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def _advance_plan_after_merge(job):
    """When a dispatch job merges: find the plan.yaml task linked via `dispatch: <job id>`,
    set it `state: done` + `done_date` (from the merge timestamp already on the job — no live
    clock), then recompute status.yaml's `next_target` = the title of the top `state: next`
    task. Defensive: no plan.yaml, or no matching task → do nothing, never crash a merge."""
    try:
        sidecar_dir = job.get("sidecar_dir") or job["cwd"]
        pf = os.path.join(sidecar_dir, "plan.yaml")
        if not os.path.exists(pf):
            return
        with open(pf, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"^tasks:\s*\n", text, re.MULTILINE)
        if not m:
            return
        tasks_start = m.end()
        tail_m = re.search(r"\n\S", text[tasks_start:])
        tasks_end = tasks_start + tail_m.start() + 1 if tail_m else len(text)
        tasks_text = text[tasks_start:tasks_end]

        hit = _find_task_block(tasks_text, job["id"])
        if not hit:
            return                              # no task links to this job → nothing to advance
        start, end, block = hit
        indent = re.match(r"[ \t]*", block).group(0)
        # docs/scheduling.md §6: a merge closes a SESSION, NOT the task. The task moves to
        # in_progress and flags a remainder review — the operator decides when it is actually done (when he
        # confirms remaining is 0). So the merge no longer auto-completes the task.
        block = _set_block_field(block, "state", "in_progress", indent)
        block = _set_block_field(block, "review_pending", "true", indent)
        # ...and it closes the SESSION, which this hook never actually did.
        # dispatcher.mark_session_done's docstring names this hook as its dispatch-side caller
        # ("manual: the operator clicks it; dispatch: the merge hook"), but the hook only ever set the two
        # fields above. So no session was ever booked by a merge: the panel read "0 / N sessions
        # done", nothing burned down, and — because state.py derives the review prefill as
        # `estimate − hours of the done sessions` — the remainder review offered the FULL estimate
        # back as the suggested remaining, on a task that had just been completed.
        # Book the first scheduled session not already closed. `sessions:` is in plan order, and
        # the id pattern cannot collide with the task's own `- id:` line.
        done_ids = _sessions_done_ids(block)
        nxt = next((s for s in re.findall(r"\bid:\s*(sess_\w+)", block) if s not in done_ids), None)
        if nxt:
            done_ids.append(nxt)
            block = _set_block_field(block, "sessions_done", "[" + ", ".join(done_ids) + "]", indent)
        tasks_text = tasks_text[:start] + block + tasks_text[end:]
        text = text[:tasks_start] + tasks_text + text[tasks_end:]

        # recompute next_target = title of the top `state: next` task (file order = priority order)
        next_title = None
        for tm in _TASK_BLOCK_RE.finditer(tasks_text):
            tb = tm.group(0)
            if _task_field(tb, "state") == "next":
                next_title = _task_field(tb, "title") or _task_field(tb, "id")
                break

        with open(pf, "w", encoding="utf-8") as fh:
            fh.write(text)

        if next_title:
            sf = os.path.join(sidecar_dir, "status.yaml")
            if os.path.exists(sf):
                with open(sf, encoding="utf-8") as fh:
                    stext = fh.read()
                stext = _set_yaml_scalar(stext, "next_target", next_title)
                with open(sf, "w", encoding="utf-8") as fh:
                    fh.write(stext)
    except Exception as e:  # noqa: BLE001 — the hook is best-effort, never fail the merge
        print(f"  ! plan.yaml advance skipped: {e}")


def do_merge(job):
    """Run the REAL merge of atlas/<id> into the project's base branch (worker owns git). On
    success: status→merged, bump status.yaml, reclaim the worktree/branch. On conflict: abort
    and route back to `ready` with a note so the operator can resolve it manually."""
    cwd, branch = job.get("cwd"), job.get("branch")
    if not (job.get("is_git") and branch):     # non-git target → nothing to merge, just record it
        job["status"] = "merged"
        _update_status_after_merge(job)
        return
    r = subprocess.run(["git", "-C", cwd, "merge", "--no-ff", branch, "-m",
                        f"Merge {branch} (Atlas job {job['id']})"],
                       capture_output=True, text=True)
    if r.returncode != 0:                       # conflict/error → abort, leave the branch, back to ready
        subprocess.run(["git", "-C", cwd, "merge", "--abort"], capture_output=True, text=True)
        job["status"] = "ready"
        job["error"] = ("merge conflict — resolve manually, then ‘mark merged’:\n"
                        + (r.stdout or "") + (r.stderr or "")).strip()[:1500]
        return
    job["status"], job["merged_at"], job["error"] = "merged", _now(), None
    _update_status_after_merge(job)
    cleanup_job(job)                            # the branch is merged → reclaim worktree + branch
    job["worktree_cleaned"] = True


def regate_job(job):
    """Rebase-on-merge re-gate. A sibling job merged and MOVED this project's base; this job was
    committed and tested against the OLD base, so a textually-clean merge can still hide semantic
    staleness (it was built for a world where the sibling's change didn't exist). Rebase its branch
    onto the new base and RE-RUN THE HARD TEST GATE:
      - rebase conflict            -> needs_input (the operator reconciles the branch)
      - tests fail after rebasing  -> needs_input (the staleness a clean merge would have hidden)
      - green (or no test_cmd)      -> stays `ready`, now fresh against the new base
    Tests-only by design: the content was already reviewer-approved once; what a base-move can break
    is behavioural, which the objective test gate catches. Caller clears the `regate` flag after."""
    cwd, branch, sandbox = job.get("cwd"), job.get("branch"), job.get("sandbox")
    if not (job.get("is_git") and branch and sandbox and os.path.isdir(sandbox)):
        return                                   # non-git, or worktree reclaimed → the merge is the backstop
    base = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["git", "-C", sandbox, "rebase", base], capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["git", "-C", sandbox, "rebase", "--abort"], capture_output=True, text=True)
        job["status"] = "needs_input"
        job["question"] = ("A sibling job merged and moved the base; this branch no longer rebases "
                           "cleanly onto it. Reconcile the branch, then mark it ready / merge.")
        return
    passed, tail = run_tests(job)
    if passed is False:
        job["status"] = "needs_input"
        job["test_output"] = (tail[-4000:] or None) if tail else None
        job["question"] = ("A sibling job merged; after rebasing onto the new base this job's tests "
                           "now FAIL — semantic staleness a clean merge would have hidden. Advise.")
        return
    job["commit"] = subprocess.run(["git", "-C", sandbox, "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
    job["regated_at"] = _now()                   # stays `ready`, freshly gated against the new base


def _flag_siblings_for_regate(merged_job):
    """After a job merges and moves its project's base, flag every sibling READY job on the SAME
    project for a rebase-on-merge re-gate (docs/agentic-structure.md §cross-session)."""
    proj = merged_job.get("project")
    for j in _all_jobs():
        if (j["id"] != merged_job["id"] and j.get("project") == proj
                and j.get("status") == "ready" and j.get("is_git") and j.get("branch")
                and not j.get("regate")):
            j["regate"] = True
            save(j)
            print(f"[{_now()}] {j['id']} flagged for rebase-on-merge re-gate (base moved by {merged_job['id']})")


def _read_textfile(path):
    """Read a text file as UTF-8; return None for binary/oversized/unreadable (so the vault diff
    skips it). 512KB cap keeps a stray large file out of the diff."""
    try:
        if os.path.getsize(path) > 512 * 1024:
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def vault_write_diff(job):
    """For a non-git vault WRITE that ran in place: diff the pre-run backup snapshot against the
    live vault so the operator can review + Keep/Revert. Returns {files, patch} or None (nothing changed).
    Text files only, capped — the analyst only touches status.yaml + log.md, so this stays small."""
    backup, cwd = job.get("sandbox"), job.get("cwd")
    if not (backup and cwd and os.path.isdir(backup)):
        return None
    IGNORE = {".git", "__pycache__", ".obsidian", ".venv", "node_modules"}
    files, patches = [], []
    for root, dirs, names in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for fn in names:
            cur = os.path.join(root, fn)
            rel = os.path.relpath(cur, cwd)
            cur_txt = _read_textfile(cur)
            if cur_txt is None:
                continue
            bak = os.path.join(backup, rel)
            bak_txt = _read_textfile(bak) if os.path.exists(bak) else ""
            if bak_txt is None:
                bak_txt = ""
            if cur_txt != bak_txt:
                files.append(rel)
                d = difflib.unified_diff(bak_txt.splitlines(), cur_txt.splitlines(),
                                         fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
                patches.append("\n".join(list(d)[:400]))
    if not files:
        return None
    return {"files": files, "patch": ("\n\n".join(patches))[:12000]}


# --- lanes -----------------------------------------------------------------------------
# A LANE is one worker's slice of a job: its own brief, its own worktree/branch, its own
# claude session, its own test + review gate. The serial engine is the one-lane case.
#
# A lane deliberately carries the same key NAMES as a job for everything the gate helpers
# read (cwd/is_git/sandbox/test_cmd/task/contract/...), so run_tests / get_diff / do_commit /
# diffstat / run_reviewer / vault_write_diff work on a lane unchanged. What differs is the
# mutable per-lane state: `state` (not `status`), plus its own sandbox, session_id and
# rework_count. `id` stays the JOB id so logs and commit messages remain job-scoped; the log
# sections are prefixed with `lane_id` so one job's log still reads as one story.
LANE_INHERITS = ("id", "cwd", "is_git", "read_only", "model", "effort", "test_cmd",
                 "review_model", "review_effort", "contract", "acceptance_criteria",
                 "permission_mode", "project")


def make_lanes(job):
    """Build the job's lanes from its confirmed contract's plan.

    No plan (the common case) → exactly ONE implicit lane whose brief is the job's task and
    whose sandbox is the job's own — i.e. bit-for-bit the pre-fan-out serial behaviour.
    """
    steps = normalize_plan((job.get("contract") or {}).get("plan"))
    if not steps:
        steps = [{"id": "s1", "brief": job.get("task") or "", "paths": [], "depends_on": []}]
        implicit = True
    else:
        implicit = False
    lanes = []
    for st in steps:
        lane = {k: job.get(k) for k in LANE_INHERITS}
        lane.update({
            "lane_id": st["id"],
            "agent": st.get("agent"),
            "implicit": implicit,
            # the lane's OWN definition of done (falls back to the session's when the scoper
            # didn't split them — run_reviewer then scopes them in the prompt instead). Read the
            # contract directly too: the job's denormalized copy only exists post-confirm.
            "acceptance_criteria": (st.get("acceptance_criteria")
                                    or job.get("acceptance_criteria")
                                    or (job.get("contract") or {}).get("acceptance_criteria")
                                    or []),
            "task": st["brief"],          # the worker's brief IS this lane's task
            "paths": st["paths"],
            "depends_on": list(st["depends_on"]),
            "state": "queued",
            "sandbox": job.get("sandbox") if implicit else None,
            "branch": job.get("branch") if implicit else None,
            "session_id": job.get("session_id") if implicit else None,
            "rework_count": job.get("rework_count", 0) if implicit else 0,
            "tokens": 0,
            "test_output": None, "review_verdict": None, "review_notes": None,
            "diffstat": None, "commit": None, "question": None, "error": None,
        })
        lanes.append(lane)
    return lanes


# Fields a finished lane mirrors up onto the job. For the single-lane case this makes the job
# record identical to what the serial engine wrote, so nothing downstream (dashboard, control center,
# dispatcher, voice) has to know lanes exist.
# CARRIED state — only mirrored once the lane actually has it, so a not-yet-started lane never
# wipes what the job already knows (its sandbox, its coding session, an earlier commit).
LANE_MIRRORS = ("sandbox", "branch", "session_id", "rework_count", "test_output",
                "review_verdict", "review_notes", "diffstat", "commit")
# CLEARABLE state — mirrored even when None, because None is meaningful: it clears a stale
# escalation. A job that parked on a question, got answered, then passed the gate must stop
# advertising that question.
LANE_MIRRORS_CLEARABLE = ("question", "error")


def mirror_lane(job, lane):
    """Copy a lane's outcome onto the job (single-lane / serial view)."""
    for k in LANE_MIRRORS:
        if lane.get(k) is not None:
            job[k] = lane[k]
    for k in LANE_MIRRORS_CLEARABLE:
        if k in lane:
            job[k] = lane[k]
    job["status"] = lane["state"]
    for k in ("report", "review", "vault_diff", "no_test_gate"):  # gate side-channels set on the lane
        if lane.get(k) is not None:
            job[k] = lane[k]
    return job


def run_gate(job):
    """Serial (single-lane) Stage-4 gate — the original entry point, behaviour preserved.

    Wraps the lane engine: build the job's one implicit lane, gate it, mirror the outcome back
    onto the job. Multi-lane jobs go through the scheduler instead, which calls run_lane_gate
    per lane directly.
    """
    lane = make_lanes(job)[0]

    def _persist():
        mirror_lane(job, lane)
        fold_lane_tokens(job, lane)
        save(job)

    run_lane_gate(lane, _persist)
    mirror_lane(job, lane)
    fold_lane_tokens(job, lane)
    return lane


def fold_lane_tokens(job, lane):
    """Add the lane's newly-spent tokens to the job's running total. Idempotent — safe to call
    on every persist (it folds the delta since the last fold, never the whole lane again)."""
    total = lane.get("tokens") or 0
    job["tokens"] = (job.get("tokens") or 0) + (total - lane.get("_folded", 0))
    lane["_folded"] = total


def fold_all_lane_tokens(job, lanes):
    """The job's token total = what it spent BEFORE fanning out (scoping) + every lane's total.

    Computed from scratch rather than accumulated, because a delta-fold cannot survive a
    resume: lanes are rehydrated from the job record, the in-memory 'already folded' marker is
    gone, and every lane's whole total gets added a second time. (Observed live on the first
    fanned dispatch — a resume that ran nothing still inflated the job by 2.3M tokens.)
    """
    if job.get("tokens_base") is None:                # captured once, before any lane spends
        job["tokens_base"] = job.get("tokens") or 0
    job["tokens"] = job["tokens_base"] + sum((x.get("tokens") or 0) for x in lanes)


def run_lane_gate(lane, save_cb=None):
    """Stage 4 gate engine (Phases 4–5). Drives ONE lane through testing → review →
    (rework loop, budget REWORK_BUDGET) → ready (committed) | needs_input | failed. Calls
    `save_cb` at each transition so the dashboard reflects live progress."""
    job = lane                              # the lane IS the gate context (see LANE_INHERITS)
    save = save_cb or (lambda: None)        # noqa: A001 — shadows the module save() by design
    while True:
        # A read-only procedure (a report — e.g. paper-trade-summary) changed nothing, so there is
        # nothing to gate or review. Park it as a REPORT: the result IS the deliverable, and the operator
        # reads it + dismisses (no approve/discard-a-change framing).
        if job.get("read_only"):
            job["state"], job["report"], job["question"] = "needs_input", True, None
            return

        # --- objective hard gate: tests ---
        job["state"] = "testing"
        save()
        passed, tail = run_tests(job)
        job["test_output"] = (tail[-4000:] or None) if tail else None

        if passed is False:                 # tests fail → straight to rework (skip the reviewer)
            verdict, summary = "needs-changes", "the project's tests are failing"
            findings = ["Make the failing tests pass.", "Test output (tail):", (tail or "")[-2500:]]
        elif not (job.get("is_git") and job.get("sandbox")):
            # non-git vault WRITE: capture the pre/post diff and park as a REVIEW — the operator reads the
            # actual change (status.yaml/log.md) and chooses Keep or Revert (a real restore from the
            # backup). This is the vault-write analogue of the code gate.
            job["state"], job["review"], job["question"] = "needs_input", True, None
            job["vault_diff"] = vault_write_diff(job)
            return
        else:                               # tests passed (or none) → judgment gate: reviewer
            job["state"] = "review"
            save()
            diff = get_diff(job)
            test_summary = "PASSED" if passed else "no test_cmd for this project (review-only gate)"
            v = run_reviewer(job, diff, test_summary)
            if not v:                       # unparseable / failed reviewer → fail safe
                verdict, summary, findings = "escalate", "reviewer returned no usable verdict", []
            else:
                verdict, summary, findings = v["verdict"], v["summary"], v["findings"]
            job["review_verdict"] = verdict
            job["review_notes"] = (summary + ("\n- " + "\n- ".join(findings) if findings else "")).strip()
        save()

        # --- decide ---
        if verdict == "commit-ready":
            if passed is True:              # hard gate satisfied → commit onto atlas/<id>
                do_commit(job, summary)
                job["state"], job["question"] = "ready", None
            else:                           # no test_cmd → COMMIT onto the disposable atlas/<id>
                # branch, then let the human gate the MERGE. The commit is not a landing: the
                # branch is thrown away by cleanup_job, and do_merge (gate #2) is still the only
                # thing that reaches the base branch. Withholding it here parked the job at
                # needs_input with the work as loose worktree state — from which there was NO
                # forward path: the answer box resumes the coding session (told not to run git),
                # and a merge request ran `git merge` against a branch with no commit on it,
                # which reports "Already up to date", exits 0, and lets cleanup_job --force the
                # reviewed work away. `ready` is what the control center already calls "committed on
                # atlas/<id>", so this makes the no-test-gate path match its own UI.
                # NOTE: do_commit captures the diffstat itself, BEFORE committing — do not set
                # job["diffstat"] after it here or `git diff HEAD` (now clean) blanks it.
                do_commit(job, summary)
                job["state"], job["question"] = "ready", None
                job["no_test_gate"] = True  # reviewer-only verdict — the control center badges it
            return
        if verdict == "escalate":
            job["state"] = "needs_input"
            job["question"] = summary or "Reviewer escalated — your input is needed."
            job["diffstat"] = diffstat(job)
            return
        # needs-changes (review) or failing tests
        if job.get("rework_count", 0) >= REWORK_BUDGET:
            job["state"] = "needs_input"
            job["question"] = (f"Gate not passed after {REWORK_BUDGET} rework attempts "
                               f"({summary}). Review the findings and advise, or take it from here.")
            job["diffstat"] = diffstat(job)
            return

        # --- rework: resume the CODER session with the findings, then re-run the gate ---
        job["rework_count"] = job.get("rework_count", 0) + 1
        job["state"] = "reworking"
        save()
        ok, text, data = _gate_invoke(job, f"{lane['lane_id']} REWORK #{job['rework_count']}",
                                      build_rework_message(summary, findings),
                                      job.get("model"), job.get("effort"),
                                      resume=job.get("session_id"))
        if ok:
            job["session_id"] = data.get("session_id") or job.get("session_id")
        else:
            job["state"], job["error"] = "failed", "rework session failed"
            return
        q = detect_needs_input(text)
        if q:                               # the coder itself escalated during rework
            job["state"], job["question"] = "needs_input", q
            job["diffstat"] = diffstat(job)
            return
        # loop → re-test / re-review


# =======================================================================================
# MULTI-WORKER DISPATCH — the deterministic orchestrator (rung 0: no LLM anywhere below)
#
# When the confirmed contract declares a plan of independent steps, the job fans out: one
# worker per step, each in its OWN worktree with its OWN test + review gate, run concurrently
# where the plan allows, then JOINED into a single branch that is re-gated as a whole before
# The operator sees one thing to merge.
#
# Every decision here is a rule, not a judgment — which is why it needs no model:
#   what may run now      -> dependencies satisfied (ready-set)
#   what may run together -> declared paths don't intersect
#   what happens on red   -> the locked gate rules, per lane
#   what the operator sees        -> one branch, one merge, after an integration re-gate
# =======================================================================================

MAX_LANES = int(os.environ.get("ATLAS_MAX_LANES", "3"))   # machine-wide concurrent-worker cap
_GIT_LOCK = None                                          # serializes `git worktree add` (lazy)
LANE_ACTIVE = ("coding", "testing", "review", "reworking")
LANE_BLOCKED = ("needs_input", "failed")


def path_prefix(pat):
    """Reduce a path or glob to the longest literal prefix it can match.

    'backend/api/**' -> 'backend/api'   'app/src/*.ts' -> 'app/src'   '*.py' -> '' (everything)
    """
    s = str(pat).replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    star = next((i for i, ch in enumerate(s) if ch in "*?["), None)
    if star is None:
        return s.strip("/")                       # a literal file or directory
    head = s[:star]
    return head[:head.rfind("/")].strip("/") if "/" in head else ""


def paths_conflict(a, b):
    """True if two steps' declared paths could touch a common file.

    Deliberately CONSERVATIVE — a false 'conflict' costs parallelism, a false 'disjoint' costs
    a corrupted merge. A step that declares no paths has unknown blast radius and so conflicts
    with everything; so does a repo-wide glob.
    """
    if not a or not b:
        return True
    xs, ys = {path_prefix(p) for p in a}, {path_prefix(p) for p in b}
    for x in xs:
        for y in ys:
            if x == "" or y == "" or x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def apply_path_conflicts(lanes):
    """Turn path overlap into ordering: a lane that conflicts with an earlier one is queued
    behind it (docs/agentic-structure.md — 'disjoint = parallel, overlap = queue'; conflict cost
    beats parallel benefit). Edges only ever point BACKWARDS in plan order, so this can never
    introduce a cycle."""
    for i, later in enumerate(lanes):
        for earlier in lanes[:i]:
            if paths_conflict(later.get("paths"), earlier.get("paths")):
                if earlier["lane_id"] not in later["depends_on"]:
                    later["depends_on"].append(earlier["lane_id"])
                    later.setdefault("serialized_after", []).append(earlier["lane_id"])
    return lanes


# Per-lane fields persisted onto the job record (JSON-safe, and small enough that the control center
# can poll it). The inherited config (cwd, model, contract...) is NOT persisted — it's rebuilt
# from the job, so there's exactly one source of truth for it.
LANE_PERSIST = ("lane_id", "agent", "task", "paths", "depends_on", "serialized_after", "implicit",
                "state", "sandbox", "branch", "session_id", "rework_count", "tokens",
                "test_output", "review_verdict", "review_notes", "diffstat", "commit",
                "question", "error")


def public_lane(lane):
    return {k: lane[k] for k in LANE_PERSIST if k in lane}


def load_lanes(job):
    """Rehydrate the job's lanes: from the persisted records once the job has run (a resume),
    else built fresh from the contract's plan."""
    saved = job.get("lanes")
    if not saved:
        return apply_path_conflicts(make_lanes(job))
    lanes = []
    for rec in saved:
        lane = {k: job.get(k) for k in LANE_INHERITS}
        lane.update(rec)
        lane.setdefault("depends_on", [])
        lanes.append(lane)
    return lanes


def is_fanned(job):
    """A job fans out only when its CONFIRMED contract declares more than one step. Fan-out is
    always declared and never inferred."""
    return len(normalize_plan((job.get("contract") or {}).get("plan"))) > 1


def derive_job_state(lanes):
    """One lifecycle word for the whole session, so everything downstream keeps working without
    knowing about lanes. needs_input wins outright — the operator's attention is the priority signal
    (it drives the control center badge, the voice ask, and the notification layer)."""
    st = {x["state"] for x in lanes}
    for s in ("needs_input", "failed", "reworking", "review", "testing", "coding", "queued"):
        if s in st:
            return s
    return "ready"


def _lane_persister(job, lanes, lock):
    """Returns a save callback that snapshots every lane onto the job record atomically."""
    def _persist():
        with lock:
            fold_all_lane_tokens(job, lanes)
            job["lanes"] = [public_lane(x) for x in lanes]
            job["status"] = derive_job_state(lanes)
            blocked = [x for x in lanes if x["state"] in LANE_BLOCKED]
            job["question"] = blocked[0].get("question") if blocked else None
            job["needs_input_lane"] = blocked[0]["lane_id"] if blocked else None
            save(job)
    return _persist


def prepare_lane_sandbox(job, lane):
    """One isolated worktree + branch per lane: atlas/<job>-<lane>. Serialized — concurrent
    `git worktree add` on one repo races on the repo's index lock."""
    global _GIT_LOCK
    import threading
    if _GIT_LOCK is None:
        _GIT_LOCK = threading.Lock()
    with _GIT_LOCK:
        WORKTREES.mkdir(exist_ok=True)
        wt = str(WORKTREES / f"{job['id']}_{lane['lane_id']}")
        branch = f"atlas/{job['id']}-{lane['lane_id']}"
        subprocess.run(["git", "-C", job["cwd"], "worktree", "add", "-b", branch, wt, "HEAD"],
                       check=True, capture_output=True, text=True)
        lane["sandbox"], lane["branch"] = wt, branch
    return wt


def run_lane(job, lane, persist):
    """One lane end to end: its own coding session, then its own gate. Mirrors what process()
    does for a serial job, scoped to this lane's worktree. Runs in its OWN thread (the fan-out
    pool), so it arms its own per-thread stop signal — the job's <id>.stop marker aborts it."""
    _enter_active(job["id"])
    try:
        if not lane.get("sandbox"):
            prepare_lane_sandbox(job, lane)
        lane["state"] = "coding"
        persist()
        resume_id = lane.get("session_id") if lane.pop("resuming", False) else None
        task = lane.pop("answer", None) or lane["task"]
        if _is_design_job(job, lane):     # UI job → the coder always has the taste baseline in context
            task = f"{task}\n\n{TASTE_RUBRIC_BUILD}"
        ok, text, data = _gate_invoke(lane, f"{lane['lane_id']} CODE", task,
                                      lane.get("model"), lane.get("effort"), resume=resume_id)
        if not ok:
            lane["state"], lane["error"] = "failed", "coding session failed"
            return
        lane["session_id"] = (data or {}).get("session_id") or lane.get("session_id")
        q = detect_needs_input(text)
        if q:                                    # this worker is blocked on the operator
            lane["state"], lane["question"] = "needs_input", q
            return
        run_lane_gate(lane, persist)
    except JobStopped:
        raise
    except subprocess.TimeoutExpired:
        lane["state"], lane["error"] = "failed", f"timed out after {JOB_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001 — one lane's crash must not take the session down
        lane["state"], lane["error"] = "failed", repr(e)
    finally:
        _exit_active(job["id"])
        persist()


def run_lanes(job, lanes):
    """The ready-set scheduler. Launches every lane whose dependencies are satisfied, up to
    MAX_LANES at once, until nothing can advance.

    LOCKED FAILURE RULE: when a lane parks (needs_input) or dies (failed), we stop LAUNCHING
    new lanes but let in-flight ones finish — their work is good and their commits are kept.
    The session then parks as a whole for the operator, who answers the one lane that asked.
    """
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    lock = threading.Lock()
    persist = _lane_persister(job, lanes, lock)
    by_id = {x["lane_id"]: x for x in lanes}
    persist()

    def _ready(lane):
        return (lane["state"] == "queued"
                and all(by_id[d]["state"] == "ready" for d in lane["depends_on"] if d in by_id))

    inflight = {}
    with ThreadPoolExecutor(max_workers=max(1, MAX_LANES)) as pool:
        while True:
            blocked = any(x["state"] in LANE_BLOCKED for x in lanes)
            for lane in lanes:
                if len(inflight) >= MAX_LANES:
                    break
                if not _ready(lane):
                    continue
                # A parked sibling holds back NEW work — but NOT a lane the operator just answered.
                # That lane is the work he unblocked; refusing to run it means answering does
                # nothing whenever more than one lane is parked at once.
                if blocked and not lane.get("resuming"):
                    continue
                lane["state"] = "coding"                  # claim it before the thread starts
                inflight[pool.submit(run_lane, job, lane, persist)] = lane
            if not inflight:
                break
            done, _pending = wait(list(inflight), return_when=FIRST_COMPLETED)
            for f in done:
                inflight.pop(f)
                exc = f.exception()
                if isinstance(exc, JobStopped):
                    raise exc                              # ■ stop → unwind the whole session
    persist()
    return lanes


def integrate_lanes(job, lanes):
    """JOIN: merge every green lane onto ONE atlas/<id> branch and re-gate the integrated
    result before it becomes the operator's single merge.

    The re-gate is the point: each lane was coded and tested against the ORIGINAL base, so a
    textual no-conflict between two lanes can still hide a semantic break (lane B was written
    against a world where lane A's change didn't exist). Tests on the integrated tree catch it;
    a clean merge alone does not.
    """
    global _GIT_LOCK
    import threading
    if _GIT_LOCK is None:
        _GIT_LOCK = threading.Lock()
    wt = str(WORKTREES / job["id"])
    branch = f"atlas/{job['id']}"
    with _GIT_LOCK:
        WORKTREES.mkdir(exist_ok=True)
        # A SCOPED job already owns this exact path + branch: process() ran the scoping session in
        # worktrees/<id> on atlas/<id> before the contract was confirmed. The scoper is read-only,
        # so that checkout is disposable — reclaim it, or `worktree add -b` below fails and the JOIN
        # dies after every lane has done its real work. No-ops for an unscoped job.
        subprocess.run(["git", "-C", job["cwd"], "worktree", "remove", wt, "--force"],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", job["cwd"], "branch", "-D", branch],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", job["cwd"], "worktree", "prune"], capture_output=True, text=True)
        r = subprocess.run(["git", "-C", job["cwd"], "worktree", "add", "-b", branch, wt, "HEAD"],
                           capture_output=True, text=True)
    if r.returncode != 0:
        job["status"] = "failed"
        job["error"] = f"could not create the integration worktree: {(r.stderr or '').strip()[:500]}"
        return job
    job["sandbox"], job["branch"] = wt, branch
    # The base the integration branch starts from. Captured BEFORE any merge because the JOIN's
    # own diffstat has to be measured against it: unlike the serial path, this worktree's changes
    # arrive as COMMITS (merges), so `git diff HEAD` is empty by the time we'd ask.
    base = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    for lane in lanes:
        if not lane.get("commit"):               # a lane that changed nothing has nothing to merge
            continue
        m = subprocess.run(["git", "-C", wt, "merge", "--no-ff", "-m",
                            f"atlas: integrate {lane['lane_id']}", lane["branch"]],
                           capture_output=True, text=True)
        if m.returncode != 0:
            conflicts = subprocess.run(["git", "-C", wt, "diff", "--name-only", "--diff-filter=U"],
                                       capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", wt, "merge", "--abort"], capture_output=True, text=True)
            job["status"] = "needs_input"
            job["question"] = (
                f"Lanes conflict on merge ({lane['lane_id']}). Each passed its own gate, but they "
                f"touch the same code:\n{conflicts or (m.stderr or '').strip()[:400]}\n"
                "Advise how to reconcile, or merge the lane branches yourself.")
            _log_append(job["id"], f"JOIN CONFLICT ({lane['lane_id']})",
                        (m.stdout or "") + "\n" + (m.stderr or ""))
            return job

    # integrated hard gate — the whole, not the parts
    job["status"] = "testing"
    save(job)
    passed, tail = run_tests(job)
    job["test_output"] = (tail[-4000:] or None) if tail else None
    if passed is False:
        job["status"] = "needs_input"
        job["question"] = ("Every lane passed on its own, but the INTEGRATED result fails the test "
                           "gate — the lanes are semantically incompatible. Review the combined "
                           "diff and advise.")
        job["diffstat"] = diffstat_since(wt, base)
        return job

    job["commit"] = subprocess.run(["git", "-C", wt, "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
    job["diffstat"] = diffstat_since(wt, base)
    job["status"], job["question"] = "ready", None
    return job


def process_fanout(job, resuming=False):
    """Drive a multi-lane job: schedule the lanes, then JOIN if they all went green."""
    lanes = load_lanes(job)
    if plan_cycle([{"id": x["lane_id"], "depends_on": x["depends_on"]} for x in lanes]):
        job["status"] = "needs_input"
        job["question"] = ("The approved plan's steps depend on each other in a cycle, so no step "
                           "can start. Edit the plan's depends_on and re-confirm.")
        return job
    if resuming:                                  # the operator answered the lane that asked
        target = job.get("needs_input_lane")
        for lane in lanes:
            if lane["lane_id"] == target and lane["state"] in LANE_BLOCKED:
                lane.update({"state": "queued", "question": None, "error": None,
                             "resuming": True, "answer": job.get("answer")})
        job["answer"] = None
    run_lanes(job, lanes)
    job["lanes"] = [public_lane(x) for x in lanes]
    if all(x["state"] == "ready" for x in lanes):
        return integrate_lanes(job, lanes)
    job["status"] = derive_job_state(lanes)
    return job


def cleanup_lane_worktrees(job):
    """Reclaim every lane worktree + branch a fanned job created (the integration worktree is
    the job's own `sandbox`, cleaned by cleanup_job)."""
    cwd = job.get("cwd")
    if not (cwd and job.get("is_git")):
        return
    for rec in (job.get("lanes") or []):
        if rec.get("sandbox"):
            subprocess.run(["git", "-C", cwd, "worktree", "remove", rec["sandbox"], "--force"],
                           capture_output=True, text=True)
        if rec.get("branch"):
            subprocess.run(["git", "-C", cwd, "branch", "-D", rec["branch"]],
                           capture_output=True, text=True)
    subprocess.run(["git", "-C", cwd, "worktree", "prune"], capture_output=True, text=True)


def process_architect(job: dict, resuming: bool):
    """Run the architect: an interactive Opus/high session (NO worktree — read-only/propose-only,
    runs in the project dir). Reads only structured state (agents.yaml taxonomy + examples), never
    the project's prose (no PLAN.md). Parks as needs_input with either a question (interrogating) or
    a proposal {summary, agents_yaml, procedures_yaml} for the operator to approve."""
    jid = job["id"]
    job["status"], job["started"] = "scoping", _now()      # reuse 'scoping' as the active state
    job["proposal"] = None                                 # stale once we re-run (revise/answer turn)
    if resuming:
        job["resume"] = False
    save(job)
    run_dir = job["cwd"]
    os.makedirs(run_dir, exist_ok=True)        # new project → create its folder so claude can run there
    task = (job.get("answer") if resuming else job.get("task")) or ""
    resume_id = job.get("session_id") if resuming else None
    proc = run_claude(task, run_dir, "skip", model=job.get("model"), effort=job.get("effort"),
                      resume=resume_id, extra=[architect_directive(job)], heavy=False)
    _write_log(jid, proc, resuming)
    data = parse_result(proc.stdout)
    if proc.returncode == 0 and data and not data.get("is_error"):
        text = data.get("result") or ""
        job["session_id"] = data.get("session_id") or job.get("session_id")
        job["cost_usd"] = data.get("total_cost_usd")
        _add_tokens(job, data)
        job["result"] = text[:8000]
        prop = detect_proposal(text)
        q = detect_needs_input(text)
        if prop:                              # org ready → park for the operator's approval gate
            job["status"], job["proposal"], job["question"] = "needs_input", prop, None
        elif q:                               # still interrogating
            job["status"], job["question"] = "needs_input", q
        else:                                 # neither — ask the operator how to proceed (resumable)
            job["status"] = "needs_input"
            job["question"] = ("Architect ended without a proposal or a question. Reply with "
                               "guidance to continue, or discard.")
    else:
        job["status"] = "failed"
        job["error"] = ((data or {}).get("result") or (proc.stderr or "").strip()[:2000]
                        or f"exit code {proc.returncode}")


def process_architect_job(job: dict):
    jid = job["id"]
    stopped = False
    resuming = bool(job.get("resume"))
    verb = "resuming architect" if resuming else "architect"
    print(f"[{_now()}] {verb} {jid}  ({job['project']}: {(job.get('task') or '')[:60]}...)")
    _enter_active(jid)
    try:
        process_architect(job, resuming)
    except JobStopped:
        stopped = True
    except subprocess.TimeoutExpired:
        job["status"], job["error"] = "failed", f"timed out after {JOB_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "failed", repr(e)
    finally:
        _exit_active(jid)
        _clear_stop(jid)
        if stopped:                          # architect has no worktree → just drop the record
            cleanup_job(job)
            delete_record(jid)
            print(f"[{_now()}] {jid} -> STOPPED (record deleted)")
        else:
            job["finished"] = _now()
            save(job)
            print(f"[{_now()}] {jid} -> {job['status']}")


def process_planner(job: dict, resuming: bool):
    """Run the planner: an interactive Opus/high session (NO worktree — read-only/propose-only;
    reads structured state ONLY, never prose). Parks as needs_input with either a question
    (interrogating) or a proposal for the operator to approve.

    THREE capabilities, ONE agent (docs/scheduling.md §7) — `mode` picks the directive + the
    proposal marker/parser it answers in:
      • 'plan'       — per-project: reads that project's plan.yaml + status.yaml → plan.yaml
                       proposal {summary, plan_yaml}.
      • 'schedule'   — global: reads schedule.yaml + EVERY project's plan.yaml → schedule.yaml
                       proposal {summary, plan_yaml} (schema key kept for the shared write path).
      • 'reestimate' — per-task: reads one task's plan.yaml block + the operator's note → a revised
                       `remaining` proposal {summary, remaining}.
    """
    jid = job["id"]
    mode = job.get("mode") or "plan"
    job["status"], job["started"] = "scoping", _now()      # reuse 'scoping' as the active state
    job["proposal"] = None                                 # stale once we re-run (revise/answer turn)
    if resuming:
        job["resume"] = False
    save(job)
    run_dir = job["cwd"]
    os.makedirs(run_dir, exist_ok=True)
    task = (job.get("answer") if resuming else job.get("task")) or ""
    resume_id = job.get("session_id") if resuming else None
    if mode == "schedule":
        directive = scheduler_directive(job)
    elif mode == "reestimate":
        directive = reestimate_directive(job)
    else:
        directive = planner_directive(job)
    proc = run_claude(task, run_dir, "skip", model=job.get("model"), effort=job.get("effort"),
                      resume=resume_id, extra=[directive], heavy=False)
    _write_log(jid, proc, resuming)
    data = parse_result(proc.stdout)
    if proc.returncode == 0 and data and not data.get("is_error"):
        text = data.get("result") or ""
        job["session_id"] = data.get("session_id") or job.get("session_id")
        job["cost_usd"] = data.get("total_cost_usd")
        _add_tokens(job, data)
        job["result"] = text[:8000]
        if mode == "schedule":
            prop = detect_schedule(text)
        elif mode == "reestimate":
            prop = detect_reestimate(text)
        else:
            prop = detect_plan(text)
        q = detect_needs_input(text)
        if prop:                              # proposal ready → park for the operator's approval gate
            job["status"], job["proposal"], job["question"] = "needs_input", prop, None
        elif q:                               # still interrogating
            job["status"], job["question"] = "needs_input", q
        else:                                 # neither — ask the operator how to proceed (resumable)
            job["status"] = "needs_input"
            label = {"schedule": "Scheduling assistant"}.get(mode, "Planner")
            job["question"] = (label +
                               " ended without a proposal or a question. Reply with "
                               "guidance to continue, or discard.")
    else:
        job["status"] = "failed"
        job["error"] = ((data or {}).get("result") or (proc.stderr or "").strip()[:2000]
                        or f"exit code {proc.returncode}")


def process_planner_job(job: dict):
    jid = job["id"]
    stopped = False
    resuming = bool(job.get("resume"))
    verb = "resuming planner" if resuming else "planner"
    print(f"[{_now()}] {verb} {jid}  ({job['project']}: {(job.get('task') or '')[:60]}...)")
    _enter_active(jid)
    try:
        process_planner(job, resuming)
    except JobStopped:
        stopped = True
    except subprocess.TimeoutExpired:
        job["status"], job["error"] = "failed", f"timed out after {JOB_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "failed", repr(e)
    finally:
        _exit_active(jid)
        _clear_stop(jid)
        if stopped:                          # planner has no worktree → just drop the record
            cleanup_job(job)
            delete_record(jid)
            print(f"[{_now()}] {jid} -> STOPPED (record deleted)")
        else:
            job["finished"] = _now()
            save(job)
            print(f"[{_now()}] {jid} -> {job['status']}")


def process_vaultkeeper(job: dict, resuming: bool):
    """Run the vault-keeper: an interactive Opus/high session in the Orchard vault repo (NO worktree —
    it edits wiki/ in place, uncommitted). After the session, capture the git diff + run the
    deterministic vault-lint; park as needs_input with a proposal {summary, files, diff, lint} for
    The operator's approval (or a question if interrogating). Nothing is committed here — approve_vaultkeeper
    commits on the operator's OK, discard_vaultkeeper reverts."""
    jid = job["id"]
    job["status"], job["started"] = "scoping", _now()      # reuse 'scoping' as the active state
    job["proposal"] = None
    if resuming:
        job["resume"] = False
    save(job)
    run_dir = job["cwd"]
    task = (job.get("answer") if resuming else job.get("task")) or ""
    resume_id = job.get("session_id") if resuming else None
    proc = run_claude(task, run_dir, "skip", model=job.get("model"), effort=job.get("effort"),
                      resume=resume_id, extra=[vaultkeeper_directive(job)], heavy=False)
    _write_log(jid, proc, resuming)
    data = parse_result(proc.stdout)
    if not (proc.returncode == 0 and data and not data.get("is_error")):
        job["status"] = "failed"
        job["error"] = ((data or {}).get("result") or (proc.stderr or "").strip()[:2000]
                        or f"exit code {proc.returncode}")
        return
    text = data.get("result") or ""
    job["session_id"] = data.get("session_id") or job.get("session_id")
    job["cost_usd"] = data.get("total_cost_usd")
    _add_tokens(job, data)
    job["result"] = text[:8000]
    prop = detect_vault_proposal(text)
    q = detect_needs_input(text)
    if prop:                                  # draft ready → capture it + run the deterministic gate
        prop["diff"] = _vault_diff(run_dir)
        prop["docs"] = _vault_docs(run_dir)
        prop["lint"] = _vault_lint()
        job["status"], job["proposal"], job["question"] = "needs_input", prop, None
    elif q:                                   # still interrogating
        job["status"], job["question"] = "needs_input", q
    else:                                     # neither — ask the operator how to proceed (resumable)
        job["status"] = "needs_input"
        job["question"] = ("Vault-keeper ended without a proposal or a question. Reply with guidance "
                           "to continue, or discard.")


def process_vaultkeeper_job(job: dict):
    jid = job["id"]
    stopped = False
    resuming = bool(job.get("resume"))
    verb = "resuming vault-keeper" if resuming else "vault-keeper"
    print(f"[{_now()}] {verb} {jid}  ({(job.get('task') or '')[:60]}...)")
    _enter_active(jid)
    try:
        process_vaultkeeper(job, resuming)
    except JobStopped:
        stopped = True
    except subprocess.TimeoutExpired:
        job["status"], job["error"] = "failed", f"timed out after {JOB_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "failed", repr(e)
    finally:
        _exit_active(jid)
        _clear_stop(jid)
        if stopped:
            _vault_revert(job["cwd"])     # drop any partial draft; no worktree to clean
            cleanup_job(job)
            delete_record(jid)
            print(f"[{_now()}] {jid} -> STOPPED (vault reverted, record deleted)")
        else:
            if job.get("status") == "failed":
                _vault_revert(job["cwd"])  # partial edits from a failed run → clean up
            job["finished"] = _now()
            save(job)
            print(f"[{_now()}] {jid} -> {job['status']}")


def process(job: dict):
    if job.get("kind") == "architect":
        return process_architect_job(job)
    if job.get("kind") == "planner":
        return process_planner_job(job)
    if job.get("kind") == "vault-keeper":
        return process_vaultkeeper_job(job)
    jid = job["id"]
    stopped = False
    resuming = bool(job.get("resume"))
    # A scope job stays in its scoping phase until the operator confirms the contract; after that
    # confirm_contract() flips scope→False and it runs as a normal exec (coding) job.
    scope_phase = bool(job.get("scope")) and not job.get("contract_confirmed")
    verb = "resuming" if resuming else ("scoping" if scope_phase else "coding")
    line = job.get("answer") if resuming else job.get("task", "")
    print(f"[{_now()}] {verb} {jid}  ({job['project']}: {(line or '')[:60]}...)")
    job["status"], job["started"] = ("scoping" if scope_phase else "coding"), _now()
    if resuming:
        job["resume"] = False               # consume the flag so a re-drain won't loop
    save(job)
    _enter_active(jid)                       # arm the stop signal for this job's subprocesses
    try:
        # FAN-OUT: an approved contract with more than one plan step runs as parallel workers,
        # each with its own worktree + coding session + gate. There is no single job-level
        # coding session in that case — the lanes own the code — so branch before starting one.
        if not scope_phase and is_fanned(job):
            process_fanout(job, resuming=resuming)
            return
        task = (job.get("answer") if resuming else job.get("task")) or ""
        resume_id = job.get("session_id") if resuming else None
        # Scoping AND coding both run in the job's disposable worktree, created once on the
        # first turn and reused. Isolation is the safety boundary, so scoping needs neither
        # plan mode (which would hijack its output into an ExitPlanMode plan instead of our
        # CONTRACT/NEEDS_INPUT convention) nor the real working tree — your files are never
        # touched; scoping reads the committed HEAD checked out into the worktree.
        if job.get("sandbox"):
            run_dir = job["sandbox"]
        else:
            run_dir = prepare_sandbox(job)
            save(job)                       # persist sandbox/branch before the (long) run
        mode = job.get("permission_mode", "skip")
        extra = [scoping_directive(job.get("test_cmd"))] if scope_phase else None
        proc = run_claude(task, run_dir, mode, model=job.get("model"),
                          effort=job.get("effort"), resume=resume_id, extra=extra)
        _write_log(jid, proc, resuming)
        data = parse_result(proc.stdout)
        if proc.returncode == 0 and data and not data.get("is_error"):
            result_text = data.get("result") or ""
            job["session_id"] = data.get("session_id") or job.get("session_id")
            job["cost_usd"] = data.get("total_cost_usd")
            _add_tokens(job, data)          # accumulate tokens across all of a job's runs
            job["result"] = result_text[:8000]
            if scope_phase:
                contract = detect_contract(result_text)
                question = detect_needs_input(result_text)
                if contract:                # contract ready → park for the operator's confirm (gate #1)
                    job["status"], job["contract"], job["question"] = "needs_input", contract, None
                elif question:              # still interrogating
                    job["status"], job["question"] = "needs_input", question
                else:                       # neither — ask the operator how to proceed (resumable)
                    job["status"] = "needs_input"
                    job["question"] = ("Scoping ended without a contract or a question. Reply "
                                       "with guidance to continue, or cancel the job.")
            else:
                question = detect_needs_input(result_text)
                if question:                # the coding session is blocked on the operator — park it
                    job["status"], job["question"] = "needs_input", question
                else:                       # coding produced a diff → run the Stage-4 gate
                    run_gate(job)
        else:
            job["status"] = "failed"
            job["error"] = ((data or {}).get("result")
                            or (proc.stderr or "").strip()[:2000]
                            or f"exit code {proc.returncode}")
    except JobStopped:                       # user hit ■ stop → abandon + delete everything
        stopped = True
    except subprocess.TimeoutExpired:
        job["status"], job["error"] = "failed", f"timed out after {JOB_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "failed", repr(e)
    finally:
        _exit_active(jid)
        _clear_stop(jid)
        if stopped:                          # tear down the sandbox + delete the record
            cleanup_job(job)
            delete_record(jid)
            print(f"[{_now()}] {jid} -> STOPPED (worktree + record deleted)")
        else:
            job["finished"] = _now()
            save(job)
            print(f"[{_now()}] {jid} -> {job['status']}")


def _all_jobs():
    out = []
    for f in JOBS.glob("job_*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return sorted(out, key=lambda j: j.get("created", ""), reverse=True)


def delete_record(jid: str):
    for ext in (".json", ".log"):
        p = JOBS / f"{jid}{ext}"
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not delete {p.name}: {e}")


def cleanup_job(job: dict):
    """Reclaim a job's disk: remove its git worktree + atlas/<id> branch, or its vault
    backup snapshot. Idempotent — safe if the sandbox is already gone or already reclaimed."""
    if job.get("worktree_cleaned"):
        return
    cleanup_lane_worktrees(job)          # a fanned job also owns one worktree+branch per lane
    sb = job.get("sandbox")
    if job.get("is_git") and sb:
        cwd = job["cwd"]
        subprocess.run(["git", "-C", cwd, "worktree", "remove", sb, "--force"],
                       capture_output=True, text=True)
        if job.get("branch"):
            subprocess.run(["git", "-C", cwd, "branch", "-D", job["branch"]],
                           capture_output=True, text=True)
        subprocess.run(["git", "-C", cwd, "worktree", "prune"], capture_output=True, text=True)
    elif sb and os.path.isdir(sb):       # vault backup snapshot (ran in place)
        shutil.rmtree(sb, ignore_errors=True)


def handle_discards():
    """Process jobs the dashboard flagged for discard: clean the sandbox, delete the record.
    Skips a job that is currently IN-FLIGHT — cleaning its worktree/record mid-run would corrupt
    the running job; the proper mid-run abort is the .stop marker. It's picked up next tick once
    the run finishes (and _run_job's entry re-check means a just-discarded job never processes)."""
    for job in _all_jobs():
        if job.get("discard") and not _is_inflight(job["id"]):
            print(f"[{_now()}] discarding {job['id']} ({job.get('project')})")
            cleanup_job(job)
            delete_record(job["id"])


def _vault_restore_from_backup(job):
    """Restore the files a vault-write review changed, from the pre-run backup snapshot back into
    the live vault. Only the files vault_diff listed (status.yaml/log.md) are touched; a file the
    run newly created is removed."""
    backup, cwd = job.get("sandbox"), job.get("cwd")
    if not (backup and cwd and os.path.isdir(backup)):
        return
    for rel in ((job.get("vault_diff") or {}).get("files") or []):
        src, dst = os.path.join(backup, rel), os.path.join(cwd, rel)
        try:
            if os.path.exists(src):
                shutil.copy2(src, dst)       # restore the pre-run version
            elif os.path.exists(dst):
                os.remove(dst)               # newly created by the run → remove it
        except OSError as e:  # noqa: BLE001
            print(f"  ! vault revert of {rel} skipped: {e}")


def handle_vault_actions():
    """Resolve a vault-write REVIEW that the operator decided on (keep | revert). Keep: the change is
    already applied in place → just finalize + reclaim the backup. Revert: restore the changed
    files from the pre-run backup, then finalize. (Non-git vault writes only.)"""
    for job in _all_jobs():
        act = job.get("vault_action")
        if not act:
            continue
        job["vault_action"] = None
        if act == "revert":
            _vault_restore_from_backup(job)
            job["result"] = "Reverted — the vault was restored from the pre-run snapshot."
            print(f"[{_now()}] reverted {job['id']} ({job.get('project')})")
        else:                                # keep
            print(f"[{_now()}] kept {job['id']} ({job.get('project')})")
        job["status"], job["finished"] = "done", _now()
        cleanup_job(job)                     # reclaim the backup (change already in place)
        job["worktree_cleaned"] = True
        save(job)


def handle_merges():
    """Process `ready` jobs the operator flagged for a one-click merge (human gate #2): run the real
    git merge, bump the project status, reclaim the sandbox — or route back to ready on conflict.
    A job awaiting a rebase-on-merge re-gate is skipped until that re-gate clears its flag, so it
    never merges against a base it hasn't been re-verified on."""
    for job in _all_jobs():
        if (job.get("merge_requested") and job.get("status") == "ready"
                and not job.get("regate")):
            print(f"[{_now()}] merging {job['id']} ({job.get('project')}: {job.get('branch')})")
            job["merge_requested"] = False
            do_merge(job)
            if job.get("status") == "merged":         # base moved → sibling ready jobs must re-gate
                _flag_siblings_for_regate(job)
            job["finished"] = _now()
            save(job)
            print(f"[{_now()}] {job['id']} -> {job['status']}")


def prune(keep: int = KEEP_TERMINAL):
    """Retain only the newest `keep` finished (done/failed/merged) records; clean the rest."""
    finished = [j for j in _all_jobs() if j.get("status") in PRUNABLE]
    for job in finished[keep:]:
        print(f"[{_now()}] pruning old job {job['id']} (status {job['status']})")
        cleanup_job(job)
        delete_record(job["id"])


def sweep_worktrees(keep: int = KEEP_WORKTREES):
    """Reclaim the heavy worktree/backup of finished jobs beyond the newest `keep`, while
    KEEPING the record (diffstat/result/tokens). Worktrees are far bigger than records, so
    they get a tighter window. `ready` (awaits merge) and active jobs are untouched."""
    finished = [j for j in _all_jobs() if j.get("status") in PRUNABLE]
    for job in finished[keep:]:
        # A FANNED job holds a worktree PER LANE, and its job-level `sandbox` is only set once
        # the JOIN runs — so gating the sweep on the scalar alone would skip a job that died
        # before integrating and leak every one of its lane worktrees until the record itself
        # aged out. Ask the lanes too.
        has_disk = bool(job.get("sandbox")) or any(
            x.get("sandbox") for x in (job.get("lanes") or []))
        if has_disk and not job.get("worktree_cleaned"):
            print(f"[{_now()}] reclaiming worktree for {job['id']} (record kept)")
            cleanup_job(job)
            job["worktree_cleaned"] = True
            save(job)


def maintain():
    handle_discards()
    handle_vault_actions()
    handle_merges()
    prune()
    sweep_worktrees()


# --- the two tracks: one worker process, concurrent across jobs (Part 1) ---
# The loop no longer blocks on a job. Each admitted queued job is submitted to its track's pool
# and runs in a background thread, so a heavy dispatch can be coding while the LIGHT control-plane
# track (planner/architect/vault-keeper) stays instantly available. PART 2: up to MAX_JOBS heavy
# dispatches run at once, admitted only when their declared paths don't overlap an in-flight
# sibling on the SAME project (cross-session serialization — the within-plan rule, lifted to jobs).
_HEAVY_POOL = None
_LIGHT_POOL = None
_INFLIGHT: dict = {}                    # jid -> "light" | "heavy" (jobs running or submitted now)
_INFLIGHT_META: dict = {}              # jid -> {"project", "paths"} for WRITING heavy jobs (overlap check)
_INFLIGHT_LOCK = threading.Lock()

# --- hot-reload rollover: a merge to worker.py/dispatcher.py leaves this always-on process running
# stale code (it loads once at start). On a change we stop admitting NEW work, let in-flight jobs
# finish, then exit(0) so the supervisor (run_worker.ps1) relaunches on the new code — automating
# the manual restart. In-flight work is never killed; a parked (needs_input) job isn't in-flight, so
# it persists on disk and the fresh worker resumes it.
_ROLLOVER = False


def _code_mtime() -> float:
    """Newest mtime across the worker's own source files — a bump means new code is on disk."""
    files = [Path(__file__), HERE / "dispatcher.py"]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def _scope_phase(job) -> bool:
    """A heavy job in its read-only SCOPING phase — the scoper reads, never writes, so it can't
    conflict with a sibling and is exempt from the path-overlap check (mirrors process())."""
    return bool(job.get("scope")) and not job.get("contract_confirmed")


def job_paths(job):
    """The paths a WRITING heavy job will touch — the union across its confirmed plan's steps.
    Empty (=> conservatively conflicts with everything, so it serializes) when none are declared."""
    out = []
    for s in normalize_plan((job.get("contract") or {}).get("plan")):
        out.extend(s.get("paths") or [])
    return out


def _heavy_conflict(project, paths):
    """True if a candidate writing job on `project` overlaps any in-flight writing sibling on the
    same project. Caller holds _INFLIGHT_LOCK. Different projects never conflict → always parallel."""
    for meta in _INFLIGHT_META.values():
        if meta["project"] == project and paths_conflict(paths, meta["paths"]):
            return True
    return False


def _ensure_pools():
    global _HEAVY_POOL, _LIGHT_POOL
    if _HEAVY_POOL is None:
        _HEAVY_POOL = ThreadPoolExecutor(max_workers=max(1, MAX_JOBS), thread_name_prefix="heavy")
        _LIGHT_POOL = ThreadPoolExecutor(max_workers=max(1, MAX_LIGHT), thread_name_prefix="light")


def _is_inflight(jid) -> bool:
    with _INFLIGHT_LOCK:
        return jid in _INFLIGHT


def _run_job(jid):
    """Process one job in a pool thread, then release its in-flight slot. Re-loads the record on
    entry (it may have been discarded or already claimed between submit and run) — a stale race
    guard, same intent as the old reload."""
    try:
        try:
            job = load(jid)
        except Exception:  # noqa: BLE001 — record vanished (e.g. discarded) → nothing to do
            return
        if job.get("status") != "queued" or job.get("discard"):
            return
        process(job)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(jid, None)
            _INFLIGHT_META.pop(jid, None)


def _run_regate(jid):
    """Run one rebase-on-merge re-gate in a heavy-pool thread (git rebase + a test suite = heavy),
    then release the slot. The `regate` flag is consumed regardless of outcome."""
    try:
        try:
            job = load(jid)
        except Exception:  # noqa: BLE001
            return
        if job.get("status") != "ready" or not job.get("regate") or job.get("discard"):
            return
        _enter_active(jid)                       # arm the stop signal for its git/test subprocesses
        try:
            regate_job(job)
        except JobStopped:
            pass                                 # stopped mid-regate → leave as ready, flag cleared below
        except Exception as e:  # noqa: BLE001
            job["status"], job["error"] = "needs_input", f"re-gate error: {e!r}"
            job["question"] = "The rebase-on-merge re-gate hit an error — review the branch and advise."
        finally:
            _exit_active(jid)
            job["regate"] = False                # consumed (success stays ready; failure is needs_input)
            save(job)
            print(f"[{_now()}] re-gate {jid} -> {job['status']}")
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(jid, None)
            _INFLIGHT_META.pop(jid, None)


def _admit_heavy(jid, job):
    """Reserve a heavy in-flight slot for `job` under the global caps + same-project overlap check.
    Caller holds _INFLIGHT_LOCK. Returns True if admitted (registry updated), else False."""
    if jid in _INFLIGHT:
        return False
    n_light = sum(1 for v in _INFLIGHT.values() if v == "light")
    if (len(_INFLIGHT) - n_light) >= MAX_JOBS:            # global heavy-dispatch cap
        return False
    if not _scope_phase(job):                             # writing job: enforce same-project safety
        paths = job_paths(job)
        if _heavy_conflict(job.get("project"), paths):
            return False
        _INFLIGHT_META[jid] = {"project": job.get("project"), "paths": paths}
    _INFLIGHT[jid] = "heavy"
    return True


def drain_once():
    """Submit every admissible queued job to its track's pool, WITHOUT blocking. Per-track caps:
    the light control-plane track to MAX_LIGHT, heavy dispatch to MAX_JOBS. A WRITING heavy job is
    admitted only when its declared paths don't overlap an in-flight sibling on the same project
    (cross-session serialization). Jobs already running are skipped via the in-flight registry."""
    if _ROLLOVER:                            # code changed → draining for a hot-reload; admit nothing
        return
    JOBS.mkdir(exist_ok=True)
    _ensure_pools()
    for f in sorted(JOBS.glob("job_*.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if job.get("discard"):
            continue
        jid = job["id"]
        status = job.get("status")
        if status == "queued":
            if job.get("kind") in LIGHT_KINDS:            # LIGHT control-plane track (own pool)
                with _INFLIGHT_LOCK:
                    if jid in _INFLIGHT:
                        continue
                    n_light = sum(1 for v in _INFLIGHT.values() if v == "light")
                    if n_light >= MAX_LIGHT:
                        continue
                    _INFLIGHT[jid] = "light"
                _LIGHT_POOL.submit(_run_job, jid)
            else:                                         # HEAVY dispatch track (caps + overlap check)
                with _INFLIGHT_LOCK:
                    if not _admit_heavy(jid, job):
                        continue
                _HEAVY_POOL.submit(_run_job, jid)
        elif status == "ready" and job.get("regate"):     # rebase-on-merge re-gate (heavy: git + tests)
            with _INFLIGHT_LOCK:
                if not _admit_heavy(jid, job):
                    continue
            _HEAVY_POOL.submit(_run_regate, jid)


def _process_queued_serial():
    """Synchronous drain: process every queued job to completion, in queue order. Used by --once
    (deterministic CLI / tests) — the watch loop uses the concurrent drain_once() instead."""
    JOBS.mkdir(exist_ok=True)
    for f in sorted(JOBS.glob("job_*.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if job.get("status") == "queued" and not job.get("discard"):
            process(load(job["id"]))      # reload to avoid a stale race


# --- liveness beacon: a heartbeat the control center reads to show worker online/offline ---
# Written from a DAEMON thread (not the work loop) so it keeps ticking even while a long job
# blocks `process()` for many minutes — liveness reflects "process alive", not "loop idle".
def _write_heartbeat():
    try:
        JOBS.mkdir(exist_ok=True)
        HEARTBEAT.write_text(json.dumps({"pid": os.getpid(), "ts": _now(),
                                         "active": _active_jids()}), encoding="utf-8")
    except Exception:  # noqa: BLE001 — a heartbeat failure must never affect the work
        pass


def _start_heartbeat():
    import threading

    def loop():
        while True:
            _write_heartbeat()
            time.sleep(HEARTBEAT_SECS)
    threading.Thread(target=loop, daemon=True).start()


def _another_worker_alive() -> bool:
    """True if a FRESH beacon from a different live worker exists — prevents two workers
    double-processing the same queue (e.g. the scheduled task plus a hand-started one)."""
    try:
        d = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        if d.get("pid") == os.getpid():
            return False
        age = (dt.datetime.now() - dt.datetime.fromisoformat(d["ts"])).total_seconds()
        return age < HEARTBEAT_STALE
    except Exception:  # noqa: BLE001 — no/garbled/stale beacon => safe to start
        return False


def main():
    JOBS.mkdir(exist_ok=True)
    once = "--once" in sys.argv
    if not once and _another_worker_alive():
        print(f"[{_now()}] another worker is already running (fresh heartbeat) — exiting.")
        return
    print(f"Atlas worker | claude={CLAUDE} | jobs={JOBS} | keep={KEEP_TERMINAL} | "
          f"keep_wt={KEEP_WORKTREES} | mode={'once' if once else 'watch'}")
    if once:
        _process_queued_serial()         # deterministic serial drain for the CLI/tests
        maintain()
        return
    print(f"tracks | heavy={MAX_JOBS} | light={MAX_LIGHT} | claude<={MAX_WORKERS} | suites<={MAX_SUITES}")
    global _ROLLOVER
    code_stamp = _code_mtime()
    _start_heartbeat()
    _write_heartbeat()                   # stamp immediately so the control center sees us at once
    while True:
        if not _ROLLOVER and _code_mtime() != code_stamp:
            print(f"[{_now()}] worker/dispatcher code changed on disk — draining in-flight jobs for a hot-reload rollover")
            _ROLLOVER = True             # drain_once now admits nothing new
        if _ROLLOVER:
            with _INFLIGHT_LOCK:
                busy = len(_INFLIGHT)
            if busy == 0:
                print(f"[{_now()}] in-flight drained — exiting so the supervisor relaunches on the new code")
                return
        drain_once()                     # non-blocking: submits to the heavy + light pools (no-op while rolling over)
        maintain()
        time.sleep(2)


if __name__ == "__main__":
    main()
